from copy import deepcopy
import math
import asyncio
from typing import Awaitable, Generator
import uuid
import abc
import time
import json
import logging

from dataclasses import dataclass
from PIL.Image import Image
from pathlib import Path

from threading import Thread
from runpod import Endpoint
from runpod_comfy_client import utils
from runpod_comfy_client.mixins import LoggingMixin
from runpod_comfy_client.workflows import WorkflowTemplate


@dataclass
class WorkflowOutputImage:
    image: Image
    name: str
    subfolder: str


class WorkflowError(Exception):
    pass


class BaseWorkflowExecutor(abc.ABC):
    @abc.abstractmethod
    def submit_workflow(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        **kwargs,
    ) -> Awaitable[list[WorkflowOutputImage]]:
        pass


class RunpodComfyWorkflowExecutor(BaseWorkflowExecutor, LoggingMixin):
    def __init__(
        self,
        endpoint: Endpoint | str,
        batch_size: int = 8,
        timeout: int = 60,
        comfyui_base_dir: str = "/comfyui",
    ):
        if isinstance(endpoint, str):
            endpoint = Endpoint(endpoint)

        self.endpoint = endpoint
        self.batch_size = batch_size
        self.comfyui_base_dir = Path(comfyui_base_dir)

        self.logger.setLevel(logging.DEBUG)

    def prepare_workflow_payload(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        **kwargs,
    ):
        # This is not the RunPod job ID but just a random ID to group the input
        # images within the ComfyUI input folder.
        job_id = uuid.uuid4().hex

        input_images_dir = self.comfyui_base_dir / "input" / job_id

        template_kwargs = dict(
            input_images_dir=input_images_dir, batch_size=self.batch_size
        )

        template_kwargs.update(kwargs)

        workflow = workflow_template.render(**template_kwargs)

        batch_count = math.ceil(num_samples / self.batch_size)

        payload = {
            "input": {
                "workflow": workflow,
                "batch_count": batch_count,
                "randomize_seed": randomize_seed,
                "images": [
                    {
                        "name": f"{i:02d}.jpg",
                        "image": utils.image_to_b64(image, format="jpeg"),
                        "subfolder": job_id,
                    }
                    for i, image in enumerate(input_images)
                ],
            }
        }

        return payload

    def _merge_chunks(self, stream):
        # The worker returns JSON lines objects but in chunks. We need to combine the
        # chunks into lines and then parse the JSON.
        chunks = []

        for chunk in stream:
            if chunk.endswith("\n"):
                chunks.append(chunk[:-1])
                yield "".join(chunks)
                chunks = []
            else:
                chunks.append(chunk)

        assert not chunks, "The last chunk should be empty. If not, the worker did not send a newline character at the end of the last line."

    def submit_workflow(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        **kwargs,
    ) -> Generator[WorkflowOutputImage, None, None]:
        job = self.endpoint.run(
            self.prepare_workflow_payload(
                workflow_template=workflow_template,
                input_images=input_images,
                num_samples=num_samples,
                randomize_seed=randomize_seed,
                **kwargs,
            )
        )

        while (status := job.status()) == "IN_QUEUE":
            self.logger.debug(
                f"Job {job.job_id} is in queue. Waiting for it to start..."
            )
            time.sleep(1)

        if status == "FAILED":
            raise WorkflowError(job.output())

        self.logger.debug(f"Job {job.job_id} has started. Streaming output...")

        for output in self._merge_chunks(job.stream()):
            self.logger.debug(f"Payload size: {len(output)}")

            output = json.loads(output)

            if not output:
                continue

            if "error" in output and not ignore_errors:
                raise WorkflowError(output["error"])

            for image_item in output["images"]:
                yield WorkflowOutputImage(
                    image=utils.image_from_b64(image_item["image"]),
                    name=image_item["name"],
                    subfolder=image_item["subfolder"],
                )

        self.logger.debug(f"Stream for job {job.job_id} has ended.")

        assert job.status() == "COMPLETED"

    def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
        **kwargs,
    ) -> list[WorkflowOutputImage]:
        if loop is None:
            loop = asyncio.get_running_loop()

        input_images = deepcopy(input_images)

        future = loop.create_future()

        def handler():
            images = []

            try:
                for image in self.submit_workflow(
                    workflow_template=workflow_template,
                    input_images=input_images,
                    num_samples=num_samples,
                    randomize_seed=randomize_seed,
                    ignore_errors=ignore_errors,
                    **kwargs,
                ):
                    images.append(image)

                loop.call_soon_threadsafe(future.set_result, images)
            except KeyboardInterrupt:
                print("Keyboard interrupt received. Cancelling job...")
            except Exception as e:
                loop.call_soon_threadsafe(future.set_exception, e)

        thread = Thread(target=handler)
        thread.start()

        return future
