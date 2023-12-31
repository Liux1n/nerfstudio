# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Abstracts for the Pipeline class.
"""
from __future__ import annotations

import typing
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Type, Union, cast

import torch
import torch.distributed as dist
from PIL import Image
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TaskProgressColumn,
)
from torch import nn
from torch.nn import Parameter
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.cuda.amp.grad_scaler import GradScaler

from nerfstudio.configs import base_config as cfg
from nerfstudio.data.datamanagers.base_datamanager import (
    DataManager,
    DataManagerConfig,
    VanillaDataManager,
)
from nerfstudio.data.datamanagers.parallel_datamanager import ParallelDataManager
from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils import profiler
from nerfstudio.cameras.rays import RayBundle

import cv2
import numpy as np

def module_wrapper(ddp_or_model: Union[DDP, Model]) -> Model:
    """
    If DDP, then return the .module. Otherwise, return the model.
    """
    if isinstance(ddp_or_model, DDP):
        return cast(Model, ddp_or_model.module)
    return ddp_or_model


class Pipeline(nn.Module):
    """The intent of this class is to provide a higher level interface for the Model
    that will be easy to use for our Trainer class.

    This class will contain high level functions for the model like getting the loss
    dictionaries and visualization code. It should have ways to get the next iterations
    training loss, evaluation loss, and generate whole images for visualization. Each model
    class should be 1:1 with a pipeline that can act as a standardized interface and hide
    differences in how each model takes in and outputs data.

    This class's function is to hide the data manager and model classes from the trainer,
    worrying about:
    1) Fetching data with the data manager
    2) Feeding the model the data and fetching the loss
    Hopefully this provides a higher level interface for the trainer to use, and
    simplifying the model classes, which each may have different forward() methods
    and so on.

    Args:
        config: configuration to instantiate pipeline
        device: location to place model and data
        test_mode:
            'train': loads train/eval datasets into memory
            'test': loads train/test dataset into memory
            'inference': does not load any dataset into memory
        world_size: total number of machines available
        local_rank: rank of current machine

    Attributes:
        datamanager: The data manager that will be used
        model: The model that will be used
    """

    datamanager: DataManager
    _model: Model
    world_size: int

    @property
    def model(self):
        """Returns the unwrapped model if in ddp"""
        return module_wrapper(self._model)

    @property
    def device(self):
        """Returns the device that the model is on."""
        return self.model.device

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: Optional[bool] = None):
        is_ddp_model_state = True
        model_state = {}
        for key, value in state_dict.items():
            if key.startswith("_model."):
                # remove the "_model." prefix from key
                model_state[key[len("_model.") :]] = value
                # make sure that the "module." prefix comes from DDP,
                # rather than an attribute of the model named "module"
                if not key.startswith("_model.module."):
                    is_ddp_model_state = False
        # remove "module." prefix added by DDP
        if is_ddp_model_state:
            model_state = {key[len("module.") :]: value for key, value in model_state.items()}

        pipeline_state = {key: value for key, value in state_dict.items() if not key.startswith("_model.")}

        # # hardcoded assuming the first image is used as train set for the 2nd round training, TODO: make it a parameter 
        # model_state['field.embedding_appearance.embedding.weight'] = model_state['field.embedding_appearance.embedding.weight'][0:1, :] 

        # hardcoded selection of appearance embedding for the 2nd round training, TODO: make it a parameter
        # train_discount = 0.1
        # n_train = model_state['field.embedding_appearance.embedding.weight'].shape[0]
        # train_indices_indices = torch.linspace(0, n_train - 2, int(train_discount*n_train), dtype=int) # equally spaced indices of training indices starting and ending at 0 and n_train-2
        # train_indices_indices = torch.cat((torch.zeros(1), train_indices_indices))

        # train_indices_indices = torch.tensor([0, 1, 13, 25, 37, 49, 62]) # hardcoded for polycam_mate_floor_complementarymask_fewtraining TODO: make it robust
        # # reverse the order to see the effect of appearance embedding TODO: remove this line
        # # train_indices_indices = torch.flip(train_indices_indices, [0])
        # print(f"train_indices_indices: {train_indices_indices}")
        # model_state['field.embedding_appearance.embedding.weight'] = model_state['field.embedding_appearance.embedding.weight'][train_indices_indices, :]

        # # hardcoded selection of appearance embedding for polycam_mate_floor_hack (or _depth), TODO: make it robust
        # model_state_inpainted = model_state['field.embedding_appearance.embedding.weight'][0:1, :]
        # # repeat it for 50 times to make its size [50, 32]
        # model_state_inpainted_repeated = model_state_inpainted.repeat(50, 1)
        # # model_state_inpainted_repeated = model_state_inpainted.repeat(200, 1)
        # # concatenate it with the original appearance embedding
        # model_state['field.embedding_appearance.embedding.weight'] = torch.cat((model_state['field.embedding_appearance.embedding.weight'], model_state_inpainted_repeated), 0)
        # print(f"model_state['field.embedding_appearance.embedding.weight'].shape: {model_state['field.embedding_appearance.embedding.weight'].shape}")

        # # HARDCODED for polycam_mate_floor_partlyrepeating
        # model_state_inpainted = model_state['field.embedding_appearance.embedding.weight'][0, :]
        # i_train_inpainted = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62]
        # model_state['field.embedding_appearance.embedding.weight'][i_train_inpainted, :] = model_state_inpainted.clone()

        try:
            self.model.load_state_dict(model_state, strict=True)
        except RuntimeError:
            if not strict:
                self.model.load_state_dict(model_state, strict=False)
            else:
                raise

        super().load_state_dict(pipeline_state, strict=False)

    @profiler.time_function
    def get_train_loss_dict(self, step: int):
        """This function gets your training loss dict. This will be responsible for
        getting the next batch of data from the DataManager and interfacing with the
        Model class, feeding the data to the model's forward function.

        Args:
            step: current iteration step to update sampler if using DDP (distributed)
        """
        if self.world_size > 1 and step:
            assert self.datamanager.train_sampler is not None
            self.datamanager.train_sampler.set_epoch(step)
        ray_bundle, batch = self.datamanager.next_train(step)
        model_outputs = self.model(ray_bundle, batch)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(model_outputs, batch, metrics_dict)

        return model_outputs, loss_dict, metrics_dict

    @profiler.time_function
    def get_eval_loss_dict(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        if self.world_size > 1:
            assert self.datamanager.eval_sampler is not None
            self.datamanager.eval_sampler.set_epoch(step)
        ray_bundle, batch = self.datamanager.next_eval(step)
        model_outputs = self.model(ray_bundle, batch)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(model_outputs, batch, metrics_dict)
        self.train()
        return model_outputs, loss_dict, metrics_dict

    @abstractmethod
    @profiler.time_function
    def get_eval_image_metrics_and_images(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """

    @abstractmethod
    @profiler.time_function
    def get_average_eval_image_metrics(
        self, step: Optional[int] = None, output_path: Optional[Path] = None, get_std: bool = False
    ):
        """Iterate over all the images in the eval dataset and get the average.

        Args:
            step: current training step
            output_path: optional path to save rendered images to
            get_std: Set True if you want to return std with the mean metric.
        """

    def load_pipeline(self, loaded_state: Dict[str, Any], step: int) -> None:
        """Load the checkpoint from the given path

        Args:
            loaded_state: pre-trained model state dict
            step: training step of the loaded checkpoint
        """

    @abstractmethod
    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        """Returns the training callbacks from both the Dataloader and the Model."""

    @abstractmethod
    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Get the param groups for the pipeline.

        Returns:
            A list of dictionaries containing the pipeline's param groups.
        """


@dataclass
class VanillaPipelineConfig(cfg.InstantiateConfig):
    """Configuration for pipeline instantiation"""

    _target: Type = field(default_factory=lambda: VanillaPipeline)
    """target class to instantiate"""
    datamanager: DataManagerConfig = DataManagerConfig()
    """specifies the datamanager config"""
    model: ModelConfig = ModelConfig()
    """specifies the model config"""


class VanillaPipeline(Pipeline):
    """The pipeline class for the vanilla nerf setup of multiple cameras for one or a few scenes.

    Args:
        config: configuration to instantiate pipeline
        device: location to place model and data
        test_mode:
            'val': loads train/val datasets into memory
            'test': loads train/test dataset into memory
            'inference': does not load any dataset into memory
        world_size: total number of machines available
        local_rank: rank of current machine
        grad_scaler: gradient scaler used in the trainer

    Attributes:
        datamanager: The data manager that will be used
        model: The model that will be used
    """

    def __init__(
        self,
        config: VanillaPipelineConfig,
        device: str,
        test_mode: Literal["test", "val", "inference"] = "val",
        world_size: int = 1,
        local_rank: int = 0,
        grad_scaler: Optional[GradScaler] = None,
        load_dir: Optional[Path] = None, # added for above-table obb derivation
        base_dir: Optional[Path] = None, # added
        config_path: Optional[Path] = None, # added for eval.py
    ):
        super().__init__()
        self.config = config
        self.test_mode = test_mode

        # added for above-table obb derivation
        self.load_dir = load_dir
        self.base_dir = base_dir
        # added for eval.py
        self.config_path = config_path

        self.datamanager: DataManager = config.datamanager.setup(
            device=device, test_mode=test_mode, world_size=world_size, local_rank=local_rank, load_dir=self.load_dir, base_dir=self.base_dir, config_path=self.config_path
        )
        self.datamanager.to(device)
        # TODO(ethan): get rid of scene_bounds from the model
        assert self.datamanager.train_dataset is not None, "Missing input dataset"

        self._model = config.model.setup(
            scene_box=self.datamanager.train_dataset.scene_box,
            num_train_data=len(self.datamanager.train_dataset),
            metadata=self.datamanager.train_dataset.metadata,
            device=device,
            grad_scaler=grad_scaler,
            load_dir=self.load_dir, # added for judging whether to freeze some parameters
        )
        self.model.to(device)

        self.world_size = world_size
        if world_size > 1:
            self._model = typing.cast(Model, DDP(self._model, device_ids=[local_rank], find_unused_parameters=True))
            dist.barrier(device_ids=[local_rank])

    @property
    def device(self):
        """Returns the device that the model is on."""
        return self.model.device

    @profiler.time_function
    def get_train_loss_dict(self, step: int):
        """This function gets your training loss dict. This will be responsible for
        getting the next batch of data from the DataManager and interfacing with the
        Model class, feeding the data to the model's forward function.

        Args:
            step: current iteration step to update sampler if using DDP (distributed)
        """
        ray_bundle, batch = self.datamanager.next_train(step)
        model_outputs = self._model(ray_bundle)  # train distributed data parallel model if world_size > 1
        #print(model_outputs['rgb'].shape) # torch.Size([4096, 3])
        #print(batch)
        '''
        {'image': tensor([[0.4157, 0.2863, 0.2549],
        [0.9020, 0.8157, 0.7255],
        [0.2510, 0.1961, 0.1843],
        ...,
        [0.2706, 0.1922, 0.1451],
        [0.3882, 0.2039, 0.1333],
        [0.9137, 0.8039, 0.7373]]), 'mask': tensor([[True],
        [True],
        [True],
        ...,
        [True],
        [True],
        [True]]), 'indices': tensor([[ 77, 150,  11],
        [ 65, 423, 823],
        [ 15,  43, 623],
        ...,
        [ 28,  67, 114],
        [ 79, 662, 268],
        [ 33, 132, 457]])}
        '''
        # TODO: 
        # 1. get camera of training image with batch['indices'].
        # 2. use the camera and the world coordinate of the NSA(defined by four corners) to get the camera coordinate of the NSA using w2c = torch.inverse(c2w)
        # 3. Identify the NSA area in the training image using the camera coordinate of the NSA
        # 4. Inpaint the NSA area in the training image after it is fed into the modelpi
        # 5. Use the inpainted image as GT
        # 6. Could modify the loss function!


        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(model_outputs, batch, metrics_dict)

        return model_outputs, loss_dict, metrics_dict

    def forward(self):
        """Blank forward method

        This is an nn.Module, and so requires a forward() method normally, although in our case
        we do not need a forward() method"""
        raise NotImplementedError

    @profiler.time_function
    def get_eval_loss_dict(self, step: int) -> Tuple[Any, Dict[str, Any], Dict[str, Any]]:
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        ray_bundle, batch = self.datamanager.next_eval(step)
        model_outputs = self.model(ray_bundle)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(model_outputs, batch, metrics_dict)
        self.train()
        return model_outputs, loss_dict, metrics_dict
    
    #new function
    @profiler.time_function
    def get_surface_detection(self, step: int, ray_bundle: RayBundle) -> Tuple[Any]:
        """This function gets the results of surface detection.

        Args:
            step: current iteration step
            ray_bundle: ray bundle to pass to model
        """
        self.eval()
        # TODO：which depth to use?
        with torch.no_grad():
            model_outputs = self.model(ray_bundle) # depth / expected_depth / prop_depth_0 / prop_depth_1
            depth = model_outputs["depth"]

            # also sample the corresponding color
            color = model_outputs["rgb"] 
        
        '''
        RayBundle(origins=tensor([0.2350, 0.7207, 0.0918], device='cuda:0'), directions=tensor([-0.5048, -0.4801, -0.7174], device='cuda:0'), pixel_area=tensor([1.4408e-06], device='cuda:0'), camera_indices=tensor([0], device='cuda:0'), nears=None, fars=None, metadata={'directions_norm': tensor([1.0684], device='cuda:0')}, times=None)
        Cameras(camera_to_worlds=tensor([[-0.9044, -0.1032,  0.4140,  0.2350],
        [ 0.4046, -0.5151,  0.7556,  0.7207],
        [ 0.1353,  0.8509,  0.5076,  0.0918]]), fx=tensor([748.3732]), fy=tensor([748.0125]), cx=tensor([503.8019]), cy=tensor([387.5774]), width=tensor([1015]), height=tensor([764]), distortion_params=tensor([ 3.4626e-02, -4.4362e-02,  0.0000e+00,  0.0000e+00, -1.3047e-03,
        -5.7565e-05]), camera_type=tensor([1]), times=None, metadata=None)
        '''

        self.train()
        return depth, color


    @profiler.time_function
    def get_eval_image_metrics_and_images(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        image_idx, camera_ray_bundle, batch = self.datamanager.next_eval_image(step)
        outputs = self.model.get_outputs_for_camera_ray_bundle(camera_ray_bundle)
        metrics_dict, images_dict = self.model.get_image_metrics_and_images(outputs, batch)
        assert "image_idx" not in metrics_dict
        metrics_dict["image_idx"] = image_idx
        assert "num_rays" not in metrics_dict
        metrics_dict["num_rays"] = len(camera_ray_bundle)
        self.train()
        return metrics_dict, images_dict

    @profiler.time_function
    def get_average_eval_image_metrics(
        self, step: Optional[int] = None, output_path: Optional[Path] = None, get_std: bool = False
    ):
        """Iterate over all the images in the eval dataset and get the average.

        Args:
            step: current training step
            output_path: optional path to save rendered images to
            get_std: Set True if you want to return std with the mean metric.

        Returns:
            metrics_dict: dictionary of metrics
        """
        self.eval()
        metrics_dict_list = []
        assert isinstance(self.datamanager, (VanillaDataManager, ParallelDataManager))
        num_images = len(self.datamanager.fixed_indices_eval_dataloader)
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(
                text_format="[progress.percentage]{task.completed}/{task.total:>.0f}({task.percentage:>3.1f}%)",
                show_speed=True,
            ),
            TimeElapsedColumn(),
            MofNCompleteColumn(),
            # transient=True,
        ) as progress:
            task = progress.add_task("[green]Evaluating all eval images...", total=num_images)
            # for camera_ray_bundle, batch in self.datamanager.fixed_indices_eval_dataloader:            
            for image_idx, (camera_ray_bundle, batch) in enumerate(self.datamanager.fixed_indices_eval_dataloader):                # time this the following line
                inner_start = time()
                height, width = camera_ray_bundle.shape
                num_rays = height * width
                outputs = self.model.get_outputs_for_camera_ray_bundle(camera_ray_bundle)
                metrics_dict, images_dict = self.model.get_image_metrics_and_images(outputs, batch)

                if output_path is not None:
                    camera_indices = camera_ray_bundle.camera_indices
                    assert camera_indices is not None
                    filename = self.datamanager.fixed_indices_eval_dataloader.input_dataset.image_filenames[image_idx]
                    filename = filename.stem # don't want extension
                    # TODO: change to oriented box if necessary
                    obb = self.datamanager.object_aabb.cpu().numpy().astype(np.double) # TODO bzs
                    ((xmin, ymin, zmin), (xmax, ymax, zmax)) = obb
                    obb = np.array([
                        [xmin, ymin, zmin],
                        [xmin, ymax, zmin],
                        [xmax, ymax, zmin],
                        [xmax, ymin, zmin],
                        [xmin, ymin, zmax],
                        [xmin, ymax, zmax],
                        [xmax, ymax, zmax],
                        [xmax, ymin, zmax],
                    ]).astype(np.double)
                    T = self.datamanager.fixed_indices_eval_dataloader.input_dataset.cameras[image_idx].camera_to_worlds.cpu().numpy().astype(np.double)
                    fx = self.datamanager.fixed_indices_eval_dataloader.input_dataset.cameras[image_idx].fx.flatten()
                    fy = self.datamanager.fixed_indices_eval_dataloader.input_dataset.cameras[image_idx].fy.flatten()
                    cy = self.datamanager.fixed_indices_eval_dataloader.input_dataset.cameras[image_idx].cy.flatten()
                    cx = self.datamanager.fixed_indices_eval_dataloader.input_dataset.cameras[image_idx].cx.flatten()
                    fx = float(fx)
                    fy = float(fy)
                    cy = float(cy)
                    cx = float(cx)
                    K = np.array([
                        [fx, 0, cx],
                        [0, fy, cy],
                        [0, 0,  1],
                    ]).astype(np.double)
                    print(f"{obb=}")
                    print(f"{T=}")
                    w2c_R = T[:3, :3].T
                    w2c_R[1:, :] *= -1 # flip y and z:
                    w2c_T = -w2c_R @ T[:3, -1]
                    w2c_R = w2c_R.astype(np.double)
                    w2c_T = w2c_T.astype(np.double)
                    print(f"{w2c_R=}")
                    print(f"{w2c_T=}")
                    try:
                        uv, _ = cv2.projectPoints(obb.astype(np.double), w2c_R, w2c_T, K, None)
                        uv = uv.reshape((-1,2))
                        print(f"{uv=}")
                    except Exception as e:
                        print(e.with_traceback())
                        uv = None
                    for key, val in images_dict.items():
                        if key == "depth_raw":
                            # save the depth_raw as a npy file
                            np.save(output_path / f"{filename}_{key}.npy", val.cpu().numpy())
                        else:
                            Image.fromarray((val * 255).byte().cpu().numpy()).save(
                                # output_path / "{0:06d}-{1}.jpg".format(int(camera_indices[0, 0, 0]), key)
                                # output_path / f"{filename}_{key}.jpg"
                                output_path / f"{filename}_{key}.png"
                            )
                        if key == "img":  # this is the original + render side by side, render on the right
                            # save the render on its own for easier inpainting
                            img = (val * 255).byte().cpu().numpy()
                            h, w, _ = img.shape
                            render = img[:, w//2:, :]
                            h, w, _ = render.shape
                            Image.fromarray(render).save(
                                # name in lama format
                                # output_path / f"{filename.replace('_', '')}_render.png"
                                output_path / f"{filename.replace('_', '')}.png"
                            )
                            if uv is None:
                                continue
                            bbox_hull = cv2.convexHull(uv.astype(np.int32))
                            try:
                                print(np.sum(bbox_hull))
                            except Exception:
                                print("oops")
                            bbox_mask = np.zeros((h, w), dtype=np.uint8)
                            cv2.fillPoly(bbox_mask, [bbox_hull], 255, cv2.LINE_AA)
                            np.putmask(render[..., 0], bbox_mask, 255)
                            Image.fromarray(render).save(
                                # name in lama format
                                output_path / f"{filename.replace('_', '')}_bbox.png"
                            )

                assert "num_rays_per_sec" not in metrics_dict
                metrics_dict["num_rays_per_sec"] = num_rays / (time() - inner_start)
                fps_str = "fps"
                assert fps_str not in metrics_dict
                metrics_dict[fps_str] = metrics_dict["num_rays_per_sec"] / (height * width)
                metrics_dict_list.append(metrics_dict)
                progress.advance(task)
        # average the metrics list
        metrics_dict = {}
        for key in metrics_dict_list[0].keys():
            if get_std:
                key_std, key_mean = torch.std_mean(
                    torch.tensor([metrics_dict[key] for metrics_dict in metrics_dict_list])
                )
                metrics_dict[key] = float(key_mean)
                metrics_dict[f"{key}_std"] = float(key_std)
            else:
                metrics_dict[key] = float(
                    torch.mean(torch.tensor([metrics_dict[key] for metrics_dict in metrics_dict_list]))
                )
        self.train()
        return metrics_dict

    def load_pipeline(self, loaded_state: Dict[str, Any], step: int) -> None:
        """Load the checkpoint from the given path

        Args:
            loaded_state: pre-trained model state dict
            step: training step of the loaded checkpoint
        """
        state = {
            (key[len("module.") :] if key.startswith("module.") else key): value for key, value in loaded_state.items()
        }
        self.model.update_to_step(step)
        self.load_state_dict(state)

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        """Returns the training callbacks from both the Dataloader and the Model."""
        datamanager_callbacks = self.datamanager.get_training_callbacks(training_callback_attributes)
        model_callbacks = self.model.get_training_callbacks(training_callback_attributes)
        callbacks = datamanager_callbacks + model_callbacks
        return callbacks

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Get the param groups for the pipeline.

        Returns:
            A list of dictionaries containing the pipeline's param groups.
        """
        datamanager_params = self.datamanager.get_param_groups()
        model_params = self.model.get_param_groups()
        # TODO(ethan): assert that key names don't overlap
        return {**datamanager_params, **model_params}
