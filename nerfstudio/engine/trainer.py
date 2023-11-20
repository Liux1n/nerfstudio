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
Code to train model.
"""
from __future__ import annotations

import dataclasses
import functools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Literal, Optional, Tuple, Type, cast
import wandb
import torch
from nerfstudio.configs.experiment_config import ExperimentConfig
from nerfstudio.data.datamanagers.base_datamanager import VanillaDataManager
from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes, TrainingCallbackLocation
from nerfstudio.engine.optimizers import Optimizers
from nerfstudio.pipelines.base_pipeline import VanillaPipeline
from nerfstudio.utils import profiler, writer
from nerfstudio.utils.decorators import check_eval_enabled, check_main_thread, check_viewer_enabled
from nerfstudio.utils.misc import step_check
from nerfstudio.utils.rich_utils import CONSOLE
from nerfstudio.utils.writer import EventName, TimeWriter
from nerfstudio.viewer.server.viewer_state import ViewerState
from nerfstudio.viewer_beta.viewer import Viewer as ViewerBetaState
from rich import box, style
from rich.panel import Panel
from rich.table import Table
from torch.cuda.amp.grad_scaler import GradScaler
from sklearn.linear_model import LinearRegression
import numpy as np

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D


TRAIN_INTERATION_OUTPUT = Tuple[torch.Tensor, Dict[str, torch.Tensor], Dict[str, torch.Tensor]]
TORCH_DEVICE = str


@dataclass
class TrainerConfig(ExperimentConfig):
    """Configuration for training regimen"""

    _target: Type = field(default_factory=lambda: Trainer)
    """target class to instantiate"""
    steps_per_save: int = 1000
    """Number of steps between saves."""
    steps_per_eval_batch: int = 500
    """Number of steps between randomly sampled batches of rays."""
    steps_per_eval_image: int = 500
    """Number of steps between single eval images."""
    steps_per_eval_all_images: int = 25000
    """Number of steps between eval all images."""
    max_num_iterations: int = 1000000
    """Maximum number of iterations to run."""
    mixed_precision: bool = False
    """Whether or not to use mixed precision for training."""
    use_grad_scaler: bool = False
    """Use gradient scaler even if the automatic mixed precision is disabled."""
    save_only_latest_checkpoint: bool = True
    """Whether to only save the latest checkpoint or all checkpoints."""
    # optional parameters if we want to resume training
    load_dir: Optional[Path] = None
    """Optionally specify a pre-trained model directory to load from."""
    load_step: Optional[int] = None
    """Optionally specify model step to load from; if none, will find most recent model in load_dir."""
    load_config: Optional[Path] = None
    """Path to config YAML file."""
    load_checkpoint: Optional[Path] = None
    """Path to checkpoint file."""
    log_gradients: bool = False
    """Optionally log gradients during training"""
    gradient_accumulation_steps: int = 1
    """Number of steps to accumulate gradients over."""


class Trainer:
    """Trainer class

    Args:
        config: The configuration object.
        local_rank: Local rank of the process.
        world_size: World size of the process.

    Attributes:
        config: The configuration object.
        local_rank: Local rank of the process.
        world_size: World size of the process.
        device: The device to run the training on.
        pipeline: The pipeline object.
        optimizers: The optimizers object.
        callbacks: The callbacks object.
        training_state: Current model training state.
    """

    pipeline: VanillaPipeline
    optimizers: Optimizers
    callbacks: List[TrainingCallback]

    def __init__(self, config: TrainerConfig, local_rank: int = 0, world_size: int = 1) -> None:
        self.train_lock = Lock()
        self.config = config
        self.local_rank = local_rank
        self.world_size = world_size
        self.device: TORCH_DEVICE = config.machine.device_type
        if self.device == "cuda":
            self.device += f":{local_rank}"
        self.mixed_precision: bool = self.config.mixed_precision
        self.use_grad_scaler: bool = self.mixed_precision or self.config.use_grad_scaler
        self.training_state: Literal["training", "paused", "completed"] = "training"
        self.gradient_accumulation_steps: int = self.config.gradient_accumulation_steps

        if self.device == "cpu":
            self.mixed_precision = False
            CONSOLE.print("Mixed precision is disabled for CPU training.")
        self._start_step: int = 0
        # optimizers
        self.grad_scaler = GradScaler(enabled=self.use_grad_scaler)

        self.base_dir: Path = config.get_base_dir()
        # directory to save checkpoints
        self.checkpoint_dir: Path = config.get_checkpoint_dir()
        CONSOLE.log(f"Saving checkpoints to: {self.checkpoint_dir}")

        self.viewer_state = None

    def setup(self, test_mode: Literal["test", "val", "inference"] = "val") -> None:
        """Setup the Trainer by calling other setup functions.

        Args:
            test_mode:
                'val': loads train/val datasets into memory
                'test': loads train/test datasets into memory
                'inference': does not load any dataset into memory
        """
        self.pipeline = self.config.pipeline.setup(
            device=self.device,
            test_mode=test_mode,
            world_size=self.world_size,
            local_rank=self.local_rank,
            grad_scaler=self.grad_scaler,
        )
        self.optimizers = self.setup_optimizers()

        # set up viewer if enabled
        viewer_log_path = self.base_dir / self.config.viewer.relative_log_filename
        self.viewer_state, banner_messages = None, None
        if self.config.is_viewer_enabled() and self.local_rank == 0:
            datapath = self.config.data
            if datapath is None:
                datapath = self.base_dir
            self.viewer_state = ViewerState(
                self.config.viewer,
                log_filename=viewer_log_path,
                datapath=datapath,
                pipeline=self.pipeline,
                trainer=self,
                train_lock=self.train_lock,
            )
            banner_messages = [f"Viewer at: {self.viewer_state.viewer_url}"]
        if self.config.is_viewer_beta_enabled() and self.local_rank == 0:
            datapath = self.config.data
            if datapath is None:
                datapath = self.base_dir
            self.viewer_state = ViewerBetaState(
                self.config.viewer,
                log_filename=viewer_log_path,
                datapath=datapath,
                pipeline=self.pipeline,
                trainer=self,
                train_lock=self.train_lock,
                share=self.config.viewer.make_share_url,
            )
            banner_messages = [f"Viewer Beta at: {self.viewer_state.viewer_url}"]
        self._check_viewer_warnings()

        self._load_checkpoint()

        self.callbacks = self.pipeline.get_training_callbacks(
            TrainingCallbackAttributes(
                optimizers=self.optimizers,
                grad_scaler=self.grad_scaler,
                pipeline=self.pipeline,
            )
        )

        # set up writers/profilers if enabled
        writer_log_path = self.base_dir / self.config.logging.relative_log_dir
        writer.setup_event_writer(
            self.config.is_wandb_enabled(),
            self.config.is_tensorboard_enabled(),
            self.config.is_comet_enabled(),
            log_dir=writer_log_path,
            experiment_name=self.config.experiment_name,
            project_name=self.config.project_name,
        )
        writer.setup_local_writer(
            self.config.logging, max_iter=self.config.max_num_iterations, banner_messages=banner_messages
        )
        writer.put_config(name="config", config_dict=dataclasses.asdict(self.config), step=0)
        profiler.setup_profiler(self.config.logging, writer_log_path)

    def setup_optimizers(self) -> Optimizers:
        """Helper to set up the optimizers

        Returns:
            The optimizers object given the trainer config.
        """
        optimizer_config = self.config.optimizers.copy()
        param_groups = self.pipeline.get_param_groups()
        return Optimizers(optimizer_config, param_groups)

    def train(self) -> None:
        """Train the model."""
        assert self.pipeline.datamanager.train_dataset is not None, "Missing DatsetInputs"

        # don't want to call save_dataparser_transform if pipeline's datamanager does not have a dataparser
        if isinstance(self.pipeline.datamanager, VanillaDataManager):
            self.pipeline.datamanager.train_dataparser_outputs.save_dataparser_transform(
                self.base_dir / "dataparser_transforms.json"
            )

        self._init_viewer_state()
        with TimeWriter(writer, EventName.TOTAL_TRAIN_TIME):
            num_iterations = self.config.max_num_iterations
            step = 0
            for step in range(self._start_step, self._start_step + num_iterations):
                while self.training_state == "paused":
                    time.sleep(0.01)
                with self.train_lock:
                    with TimeWriter(writer, EventName.ITER_TRAIN_TIME, step=step) as train_t:
                        self.pipeline.train()

                        # training callbacks before the training iteration
                        for callback in self.callbacks:
                            callback.run_callback_at_location(
                                step, location=TrainingCallbackLocation.BEFORE_TRAIN_ITERATION
                            )

                        # time the forward pass
                        loss, loss_dict, metrics_dict = self.train_iteration(step)

                        # training callbacks after the training iteration
                        for callback in self.callbacks:
                            callback.run_callback_at_location(
                                step, location=TrainingCallbackLocation.AFTER_TRAIN_ITERATION
                            )

                # Skip the first two steps to avoid skewed timings that break the viewer rendering speed estimate.
                if step > 1:
                    writer.put_time(
                        name=EventName.TRAIN_RAYS_PER_SEC,
                        duration=self.world_size
                        * self.pipeline.datamanager.get_train_rays_per_batch()
                        / max(0.001, train_t.duration),
                        step=step,
                        avg_over_steps=True,
                    )

                self._update_viewer_state(step)

                # a batch of train rays
                if step_check(step, self.config.logging.steps_per_log, run_at_zero=True):
                    writer.put_scalar(name="Train Loss", scalar=loss, step=step)
                    writer.put_dict(name="Train Loss Dict", scalar_dict=loss_dict, step=step)
                    writer.put_dict(name="Train Metrics Dict", scalar_dict=metrics_dict, step=step)
                    # The actual memory allocated by Pytorch. This is likely less than the amount
                    # shown in nvidia-smi since some unused memory can be held by the caching
                    # allocator and some context needs to be created on GPU. See Memory management
                    # (https://pytorch.org/docs/stable/notes/cuda.html#cuda-memory-management)
                    # for more details about GPU memory management.
                    writer.put_scalar(
                        name="GPU Memory (MB)", scalar=torch.cuda.max_memory_allocated() / (1024**2), step=step
                    )
                if step == 0:
                    offset = 1
                t = 0
                # if self.pipeline.datamanager.ray_bundle_surface_detection and step == 20*offset:
                if self.pipeline.datamanager.ray_bundle_surface_detection and step % 1000 == 0:
                    output = self.pipeline.get_surface_detection(step, self.pipeline.datamanager.ray_bundle_surface_detection)

                    '''
                    RayBundle(origins=tensor([0.2350, 0.7207, 0.0918], device='cuda:0'), directions=tensor([-0.5048, -0.4801, -0.7174], device='cuda:0'), pixel_area=tensor([1.4408e-06], device='cuda:0'), camera_indices=tensor([0], device='cuda:0'), nears=None, fars=None, metadata={'directions_norm': tensor([1.0684], device='cuda:0')}, times=None)
                    Cameras(camera_to_worlds=tensor([[-0.9044, -0.1032,  0.4140,  0.2350],
                    [ 0.4046, -0.5151,  0.7556,  0.7207],
                    [ 0.1353,  0.8509,  0.5076,  0.0918]]), fx=tensor([748.3732]), fy=tensor([748.0125]), cx=tensor([503.8019]), cy=tensor([387.5774]), width=tensor([1015]), height=tensor([764]), distortion_params=tensor([ 3.4626e-02, -4.4362e-02,  0.0000e+00,  0.0000e+00, -1.3047e-03,
                    -5.7565e-05]), camera_type=tensor([1]), times=None, metadata=None)
                    '''
                    num_image = len(self.pipeline.datamanager.expanded_cameras)
                    print(num_image, len(output))
                    assert num_image == len(output) , f"false length"

                    transform_matrix = self.pipeline.datamanager.transform_matrix
                    scale_factor = self.pipeline.datamanager.scale_factor
                    world_xyz = []
                    for i in range(num_image):
                     
                        fx = self.pipeline.datamanager.expanded_cameras[i].fx
                        fy = self.pipeline.datamanager.expanded_cameras[i].fy
                        cx = self.pipeline.datamanager.expanded_cameras[i].cx
                        cy = self.pipeline.datamanager.expanded_cameras[i].cy
                        c2w = self.pipeline.datamanager.expanded_cameras[i].camera_to_worlds
                        depth = output[i].to(fx.device)
                        y = self.pipeline.datamanager.ray_indices[i][1]
                        x = self.pipeline.datamanager.ray_indices[i][2]
                        # xyz in camera coordinates
                        X = (x - cx) * depth / fx
                        # Y = (y - cy) * depth / fy
                        # Z = depth
                        # invert the YZ axis
                        Y = -(y - cy) * depth / fy
                        Z = -depth
                        # Convert to world coordinates
                        camera_xyz = torch.stack([X, Y, Z, torch.ones_like(X)], dim=-1)
                        c2w = c2w.to(camera_xyz.device)
                        #world_xyz.append((c2w @ camera_xyz.T).T[..., :3])
                        world_coordinates = (c2w @ camera_xyz.T).T[..., :3] # shape: (num_rays, 3)
                        # Transform the world coordinates
                        world_coordinates_homogeneous = torch.cat([world_coordinates, torch.ones(len(world_coordinates), 1)], dim=-1)
                        transformed_world_coordinates = (transform_matrix.to(world_coordinates_homogeneous.device) @ world_coordinates_homogeneous.T).T[..., :3]
                        scaled_transformed_world_coordinates = transformed_world_coordinates * scale_factor
                        # world_xyz.append(scaled_transformed_world_coordinates)
                        world_xyz.append(world_coordinates)
                    
                    # calculate the plane equation using linear regression
                    # Flatten the world_xyz list and convert it to a numpy array
                    world_xyz_np = np.concatenate([xyz.numpy() for xyz in world_xyz], axis=0)
                    # Create a LinearRegression object
                    reg = LinearRegression()

                    # Fit the model to the data
                    reg.fit(world_xyz_np[:, :2], world_xyz_np[:, 2])

                    # The coefficients a, b are in reg.coef_, and the intercept d is in reg.intercept_
                    a, b = reg.coef_
                    d = reg.intercept_
                    c = -1

                    # The equation of the plane is `ax + by + cz + d = 0`
                    #CONSOLE.print(f"The equation of the plane is {a}x + {b}y + {c}z + {d} = 0")

                    vertices = self.pipeline.datamanager.vertices

                    #calculate difference between planes over steps


                    t_temp = -(a*3 + b*3 + d) / c
                    
                    diff = abs(t - t_temp)
                    t = t_temp

                    # writer.put_scalar(name="Plane Difference", scalar=diff, step=step)
                    print(f"Plane Difference: {diff}")
                    # offset += 1
                    #uncomment this for visualization
                    
                    # Create a 3D plot
                    fig = plt.figure()
                    ax = fig.add_subplot(111, projection='3d')
                    ax.set_title('plane and bbox with transformation and scaling')
                    ## Plot the points in world_xyz
                    #for xyz in world_xyz:
                    #    ax.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2])

                    # Plot the plane
                    # xx, yy = np.meshgrid(range(-3, 3), range(-3, 3))
                    xx, yy = np.meshgrid(range(-2, 2), range(-2, 2))
                    zz = (-a * xx - b * yy - d) / c
                    ax.plot_surface(xx, yy, zz, alpha=0.5)

                    
                    #print(vertices.shape) # 8 x 3
                    #TODO: plot the vertices
                    edges = {
                        "x": [(0, 4), (1, 5), (2, 6), (3, 7)],
                        "y": [(0, 2), (1, 3), (4, 6), (5, 7)],
                        "z": [(0, 1), (2, 3), (4, 5), (6, 7)]
                    }
                    # Plot the edges of the bbox
                    for direction, edge_indices in edges.items():
                        for i, j in edge_indices:
                            # Get the starting and ending vertices for this edge
                            starting_vertex = vertices[i]
                            ending_vertex = vertices[j]
                            # Plot the edge
                            ax.plot([starting_vertex[0], ending_vertex[0]], [starting_vertex[1], ending_vertex[1]], [starting_vertex[2], ending_vertex[2]], color="red")
                    
                    # for vertex in vertices:
                    #     ax.scatter(*vertex)
                    # Plot the bbox
                    
                    #plt.show()
            
                    # TODO: calculate the intersection of the obb and the plane to derive the NSA
                    print(f"The points used for the plane equation are: {world_xyz_np}")
                    print(f"Plane Equation: {a}x + {b}y + {c}z + {d} = 0")
                    print(f"The object bbox vertices are: {vertices}")
                    print(f"The object bbox directional vectors are: x:{self.pipeline.datamanager.dir_x}, y:{self.pipeline.datamanager.dir_y}, z:{self.pipeline.datamanager.dir_z}")

                    bbox_intersections = self.derive_nsa(a, b, c, d, vertices, self.pipeline.datamanager.dir_x, self.pipeline.datamanager.dir_y, self.pipeline.datamanager.dir_z)
                    # plot the intersections in the 3D plot
                    for intersection in bbox_intersections:
                        ax.scatter(*intersection)
                    # save the 3D plot locally
                    plot_dir = os.path.join(self.base_dir / self.config.logging.relative_log_dir, 'wandb/plots')
                    if not os.path.exists(plot_dir):
                        os.makedirs(plot_dir)
                    plot_path = os.path.join(plot_dir, f"step-{step:09d}.png")
                    plt.savefig(plot_path)
                    # log the 3D plot at plot_path to wandb
                    # wandb.log({"3D Plot": wandb.Image(plot_path)})

                    #print(output_surface_detection) # depth / expected_depth / prop_depth_0 / prop_depth_1

                # Do not perform evaluation if there are no validation images
                if self.pipeline.datamanager.eval_dataset:
                    self.eval_iteration(step)

                if step_check(step, self.config.steps_per_save):
                    self.save_checkpoint(step)

                writer.write_out_storage()

        # save checkpoint at the end of training
        self.save_checkpoint(step)

        # write out any remaining events (e.g., total train time)
        writer.write_out_storage()

        table = Table(
            title=None,
            show_header=False,
            box=box.MINIMAL,
            title_style=style.Style(bold=True),
        )
        table.add_row("Config File", str(self.config.get_base_dir() / "config.yml"))
        table.add_row("Checkpoint Directory", str(self.checkpoint_dir))
        CONSOLE.print(Panel(table, title="[bold][green]:tada: Training Finished :tada:[/bold]", expand=False))

        # after train end callbacks
        for callback in self.callbacks:
            callback.run_callback_at_location(step=step, location=TrainingCallbackLocation.AFTER_TRAIN)

        if not self.config.viewer.quit_on_train_completion:
            self._train_complete_viewer()

    def derive_nsa(self, a, b, c, d, vertices, x_dir, y_dir, z_dir):
        # Initialize the list of intersections
        intersections = []

        # Define the edges
        edges = {
            "x": [(0, 4), (1, 5), (2, 6), (3, 7)],
            "y": [(0, 2), (1, 3), (4, 6), (5, 7)],
            "z": [(0, 1), (2, 3), (4, 5), (6, 7)]
        }

        # Iterate over each edge
        for direction, edge_indices in edges.items():
            for i, j in edge_indices:
                # Get the starting vertex and directional vector for this edge
                starting_vertex = vertices[i]
                if direction == "x":
                    directional_vector = x_dir
                elif direction == "y":
                    directional_vector = y_dir
                else:  # direction == "z"
                    directional_vector = z_dir

                # Solve for t
                t = -(a * starting_vertex[0] + b * starting_vertex[1] + c * starting_vertex[2] + d) / (a * directional_vector[0] + b * directional_vector[1] + c * directional_vector[2])

                # If t is in the range [0, 1], the edge intersects with the plane
                if 0 <= t <= 1:
                    # Calculate the 3D coordinate of the intersection point
                    intersection = starting_vertex + t * directional_vector
                    # Append the intersection to the list of intersections
                    intersections.append(intersection)

        # Return the list of intersections
        return intersections
    
    @check_main_thread
    def _check_viewer_warnings(self) -> None:
        """Helper to print out any warnings regarding the way the viewer/loggers are enabled"""
        if (
            (self.config.is_viewer_enabled() or self.config.is_viewer_beta_enabled())
            and not self.config.is_tensorboard_enabled()
            and not self.config.is_wandb_enabled()
            and not self.config.is_comet_enabled()
        ):
            string: str = (
                "[NOTE] Not running eval iterations since only viewer is enabled.\n"
                "Use [yellow]--vis {wandb, tensorboard, viewer+wandb, viewer+tensorboard}[/yellow] to run with eval."
            )
            CONSOLE.print(f"{string}")

    @check_viewer_enabled
    def _init_viewer_state(self) -> None:
        """Initializes viewer scene with given train dataset"""
        assert self.viewer_state and self.pipeline.datamanager.train_dataset
        self.viewer_state.init_scene(
            train_dataset=self.pipeline.datamanager.train_dataset,
            train_state="training",
            eval_dataset=self.pipeline.datamanager.eval_dataset,
        )

    @check_viewer_enabled
    def _update_viewer_state(self, step: int) -> None:
        """Updates the viewer state by rendering out scene with current pipeline
        Returns the time taken to render scene.

        Args:
            step: current train step
        """
        assert self.viewer_state is not None
        num_rays_per_batch: int = self.pipeline.datamanager.get_train_rays_per_batch()
        try:
            self.viewer_state.update_scene(step, num_rays_per_batch)
        except RuntimeError:
            time.sleep(0.03)  # sleep to allow buffer to reset
            CONSOLE.log("Viewer failed. Continuing training.")

    @check_viewer_enabled
    def _train_complete_viewer(self) -> None:
        """Let the viewer know that the training is complete"""
        assert self.viewer_state is not None
        self.training_state = "completed"
        try:
            self.viewer_state.training_complete()
        except RuntimeError:
            time.sleep(0.03)  # sleep to allow buffer to reset
            CONSOLE.log("Viewer failed. Continuing training.")
        CONSOLE.print("Use ctrl+c to quit", justify="center")
        while True:
            time.sleep(0.01)

    @check_viewer_enabled
    def _update_viewer_rays_per_sec(self, train_t: TimeWriter, vis_t: TimeWriter, step: int) -> None:
        """Performs update on rays/sec calculation for training

        Args:
            train_t: timer object carrying time to execute total training iteration
            vis_t: timer object carrying time to execute visualization step
            step: current step
        """
        train_num_rays_per_batch: int = self.pipeline.datamanager.get_train_rays_per_batch()
        writer.put_time(
            name=EventName.TRAIN_RAYS_PER_SEC,
            duration=self.world_size * train_num_rays_per_batch / (train_t.duration - vis_t.duration),
            step=step,
            avg_over_steps=True,
        )

    def _load_checkpoint(self) -> None:
        """Helper function to load pipeline and optimizer from prespecified checkpoint"""
        load_dir = self.config.load_dir
        load_checkpoint = self.config.load_checkpoint
        if load_dir is not None:
            load_step = self.config.load_step
            if load_step is None:
                print("Loading latest Nerfstudio checkpoint from load_dir...")
                # NOTE: this is specific to the checkpoint name format
                load_step = sorted(int(x[x.find("-") + 1 : x.find(".")]) for x in os.listdir(load_dir))[-1]
            load_path: Path = load_dir / f"step-{load_step:09d}.ckpt"
            assert load_path.exists(), f"Checkpoint {load_path} does not exist"
            loaded_state = torch.load(load_path, map_location="cpu")
            self._start_step = loaded_state["step"] + 1
            # load the checkpoints for pipeline, optimizers, and gradient scalar
            self.pipeline.load_pipeline(loaded_state["pipeline"], loaded_state["step"])
            self.optimizers.load_optimizers(loaded_state["optimizers"])
            if "schedulers" in loaded_state and self.config.load_scheduler:
                self.optimizers.load_schedulers(loaded_state["schedulers"])
            self.grad_scaler.load_state_dict(loaded_state["scalers"])
            CONSOLE.print(f"Done loading Nerfstudio checkpoint from {load_path}")
        elif load_checkpoint is not None:
            assert load_checkpoint.exists(), f"Checkpoint {load_checkpoint} does not exist"
            loaded_state = torch.load(load_checkpoint, map_location="cpu")
            self._start_step = loaded_state["step"] + 1
            # load the checkpoints for pipeline, optimizers, and gradient scalar
            self.pipeline.load_pipeline(loaded_state["pipeline"], loaded_state["step"])
            self.optimizers.load_optimizers(loaded_state["optimizers"])
            if "schedulers" in loaded_state and self.config.load_scheduler:
                self.optimizers.load_schedulers(loaded_state["schedulers"])
            self.grad_scaler.load_state_dict(loaded_state["scalers"])
            CONSOLE.print(f"Done loading Nerfstudio checkpoint from {load_checkpoint}")
        else:
            CONSOLE.print("No Nerfstudio checkpoint to load, so training from scratch.")

    @check_main_thread
    def save_checkpoint(self, step: int) -> None:
        """Save the model and optimizers

        Args:
            step: number of steps in training for given checkpoint
        """
        # possibly make the checkpoint directory
        if not self.checkpoint_dir.exists():
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # save the checkpoint
        ckpt_path: Path = self.checkpoint_dir / f"step-{step:09d}.ckpt"
        torch.save(
            {
                "step": step,
                "pipeline": self.pipeline.module.state_dict()  # type: ignore
                if hasattr(self.pipeline, "module")
                else self.pipeline.state_dict(),
                "optimizers": {k: v.state_dict() for (k, v) in self.optimizers.optimizers.items()},
                "schedulers": {k: v.state_dict() for (k, v) in self.optimizers.schedulers.items()},
                "scalers": self.grad_scaler.state_dict(),
            },
            ckpt_path,
        )
        # possibly delete old checkpoints
        if self.config.save_only_latest_checkpoint:
            # delete everything else in the checkpoint folder
            for f in self.checkpoint_dir.glob("*"):
                if f != ckpt_path:
                    f.unlink()

    @profiler.time_function
    def train_iteration(self, step: int) -> TRAIN_INTERATION_OUTPUT:
        """Run one iteration with a batch of inputs. Returns dictionary of model losses.

        Args:
            step: Current training step.
        """

        self.optimizers.zero_grad_all()
        cpu_or_cuda_str: str = self.device.split(":")[0]
        assert (
            self.gradient_accumulation_steps > 0
        ), f"gradient_accumulation_steps must be > 0, not {self.gradient_accumulation_steps}"
        for _ in range(self.gradient_accumulation_steps):
            with torch.autocast(device_type=cpu_or_cuda_str, enabled=self.mixed_precision):
                _, loss_dict, metrics_dict = self.pipeline.get_train_loss_dict(step=step)
                loss = functools.reduce(torch.add, loss_dict.values())
                loss /= self.gradient_accumulation_steps
            self.grad_scaler.scale(loss).backward()  # type: ignore
        self.optimizers.optimizer_scaler_step_all(self.grad_scaler)

        if self.config.log_gradients:
            total_grad = 0
            for tag, value in self.pipeline.model.named_parameters():
                assert tag != "Total"
                if value.grad is not None:
                    grad = value.grad.norm()
                    metrics_dict[f"Gradients/{tag}"] = grad  # type: ignore
                    total_grad += grad

            metrics_dict["Gradients/Total"] = cast(torch.Tensor, total_grad)  # type: ignore

        scale = self.grad_scaler.get_scale()
        self.grad_scaler.update()
        # If the gradient scaler is decreased, no optimization step is performed so we should not step the scheduler.
        if scale <= self.grad_scaler.get_scale():
            self.optimizers.scheduler_step_all(step)

        # Merging loss and metrics dict into a single output.
        return loss, loss_dict, metrics_dict  # type: ignore

    @check_eval_enabled
    @profiler.time_function
    def eval_iteration(self, step: int) -> None:
        """Run one iteration with different batch/image/all image evaluations depending on step size.

        Args:
            step: Current training step.
        """
        # a batch of eval rays
        if step_check(step, self.config.steps_per_eval_batch):
            _, eval_loss_dict, eval_metrics_dict = self.pipeline.get_eval_loss_dict(step=step)
            eval_loss = functools.reduce(torch.add, eval_loss_dict.values())
            writer.put_scalar(name="Eval Loss", scalar=eval_loss, step=step)
            writer.put_dict(name="Eval Loss Dict", scalar_dict=eval_loss_dict, step=step)
            writer.put_dict(name="Eval Metrics Dict", scalar_dict=eval_metrics_dict, step=step)

        # one eval image
        if step_check(step, self.config.steps_per_eval_image):
            with TimeWriter(writer, EventName.TEST_RAYS_PER_SEC, write=False) as test_t:
                metrics_dict, images_dict = self.pipeline.get_eval_image_metrics_and_images(step=step)
            writer.put_time(
                name=EventName.TEST_RAYS_PER_SEC,
                duration=metrics_dict["num_rays"] / test_t.duration,
                step=step,
                avg_over_steps=True,
            )
            writer.put_dict(name="Eval Images Metrics", scalar_dict=metrics_dict, step=step)
            group = "Eval Images"
            for image_name, image in images_dict.items():
                writer.put_image(name=group + "/" + image_name, image=image, step=step)

        # all eval images
        if step_check(step, self.config.steps_per_eval_all_images):
            metrics_dict = self.pipeline.get_average_eval_image_metrics(step=step)
            writer.put_dict(name="Eval Images Metrics Dict (all images)", scalar_dict=metrics_dict, step=step)
