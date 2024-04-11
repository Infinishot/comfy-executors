from contextlib import asynccontextmanager
from copy import deepcopy
import math
import asyncio
from typing import Awaitable, Generator
import uuid
import abc
import time
import json

from dataclasses import dataclass
from PIL.Image import Image
from pathlib import Path

from threading import Thread
from comfy_api_client import ComfyUIAPIClient
from comfy_api_client.utils import randomize_noise_seeds
import httpx
from runpod import Endpoint
from comfy_executors import utils
from comfy_executors.mixins import LoggingMixin
from comfy_executors.workflows import WorkflowTemplate


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
    ) -> Generator[WorkflowOutputImage, None, None]:
        pass

    @abc.abstractmethod
    def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
        **kwargs,
    ) -> Awaitable[list[WorkflowOutputImage]]:
        pass


class RunpodWorkflowExecutor(BaseWorkflowExecutor, LoggingMixin):
    def __init__(
        self,
        endpoint: Endpoint | str,
        batch_size: int = 1,
        comfyui_base_dir: str = "/comfyui",
    ):
        if isinstance(endpoint, str):
            endpoint = Endpoint(endpoint)

        self.endpoint = endpoint
        self.batch_size = batch_size
        self.comfyui_base_dir = Path(comfyui_base_dir)

    def _prepare_workflow_payload(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int | None = None,
        randomize_seed: bool = True,
        **kwargs,
    ):
        # This is not the RunPod job ID but just a random ID to group the input
        # images within the ComfyUI input folder.
        job_id = uuid.uuid4().hex

        input_images_dir = self.comfyui_base_dir / "input" / job_id

        batch_size = kwargs.get("batch_size", self.batch_size)

        template_kwargs = dict(input_images_dir=input_images_dir, batch_size=batch_size)

        template_kwargs.update(kwargs)

        workflow = workflow_template.render(**template_kwargs)

        if num_samples is not None:
            batch_count = math.ceil(num_samples / batch_size)
        else:
            batch_count = kwargs.get("batch_count", 1)

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
            self._prepare_workflow_payload(
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
    ) -> Awaitable[list[WorkflowOutputImage]]:
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


class RemoteWorkflowExecutor(BaseWorkflowExecutor, LoggingMixin):
    def __init__(self, comfy_host: str, client: httpx.AsyncClient, batch_size: int = 1):
        self.comfy_host = comfy_host
        self.batch_size = batch_size
        self.client = ComfyUIAPIClient(comfy_host, client)

    @classmethod
    @asynccontextmanager
    async def create(cls, comfy_host: str, **kwargs):
        async with httpx.AsyncClient() as client:
            yield cls(comfy_host=comfy_host, client=client, **kwargs)

    def submit_workflow(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        **kwargs,
    ) -> list[WorkflowOutputImage]:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
            self.submit_workflow_async(
                workflow_template=workflow_template,
                input_images=input_images,
                num_samples=num_samples,
                randomize_seed=randomize_seed,
                loop=loop,
                **kwargs,
            )
        )

    async def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
        **kwargs,
    ) -> list[WorkflowOutputImage]:
        job_id = uuid.uuid4().hex

        uploads = [
            asyncio.create_task(
                self.client.upload_image(f"{i:04d}.jpg", image, subfolder=job_id)
            )
            for i, image in enumerate(input_images)
        ]

        self.logger.info(f"Uploading {len(uploads)} images for job {job_id}...")

        await asyncio.gather(*uploads)

        self.logger.info(f"Images uploaded for job {job_id}. Submitting workflow...")

        batch_size = kwargs.get("batch_size", self.batch_size)

        workflow = workflow_template.render(
            input_images_dir=f"input/{job_id}",
            batch_size=batch_size,
            **kwargs,
        )

        if num_samples is not None:
            batch_count = math.ceil(num_samples / batch_size)
        else:
            batch_count = kwargs.get("batch_count", 1)

        prompts = []

        for _ in range(batch_count):
            submit_workflow = workflow

            if randomize_seed:
                submit_workflow = randomize_noise_seeds(submit_workflow)

            prompts.append(
                asyncio.create_task(self.client.enqueue_workflow(submit_workflow))
            )

        self.logger.info(f"Workflow submitted for job {job_id}. Waiting for results...")

        futures = [result.future for result in await asyncio.gather(*prompts)]

        outputs = []

        for i, future in enumerate(asyncio.as_completed(futures)):
            result = await future

            self.logger.info(
                f"Got result for batch {i + 1}/{batch_count} for job {job_id}"
            )

            for image_item in result.output_images:
                outputs.append(
                    WorkflowOutputImage(
                        image=image_item,
                        name=image_item.filename,
                        subfolder=None,
                    )
                )

        return outputs