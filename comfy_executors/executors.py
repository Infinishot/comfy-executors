from contextlib import asynccontextmanager
from copy import deepcopy
import math
import asyncio
import random
from typing import AsyncIterator, Awaitable, Generator
import uuid
import abc
import time
import json

from dataclasses import dataclass
from PIL import Image as ImageFactory
from PIL.Image import Image
from pathlib import Path

from threading import Thread
from comfy_api_client import ComfyUIAPIClient
from comfy_api_client import create_client as create_comfy_client
from comfy_api_client.utils import randomize_noise_seeds
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
    async def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        **kwargs,
    ) -> Awaitable[list[WorkflowOutputImage]]:
        pass


class RunPodWorkflowExecutor(BaseWorkflowExecutor, LoggingMixin):
    def __init__(
        self,
        endpoint_id: str,
        batch_size: int = 1,
        comfyui_base_dir: str = "/comfyui",
    ):
        try:
            from runpod import Endpoint
        except ImportError:
            raise ImportError(
                "The RunPodWorkflowExecutor requires the 'runpod' package to be installed."
            )

        self.endpoint = Endpoint(endpoint_id)
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
                        "name": f"{i:02d}.png",
                        "image": utils.image_to_b64(image, format="png"),
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

    async def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        **kwargs,
    ) -> Awaitable[list[WorkflowOutputImage]]:
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

        return await future


class ComfyServerWorkflowExecutor(BaseWorkflowExecutor, LoggingMixin):
    def __init__(
        self,
        comfy_client: ComfyUIAPIClient,
        batch_size: int = 1,
        input_base_dir: str = "input",
    ):
        self.comfy_client = comfy_client
        self.batch_size = batch_size
        self.input_base_dir = input_base_dir

    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        comfy_host: str,
        batch_size: int = 1,
        input_base_dir: str = "input",
        **comfy_client_kwargs,
    ):
        async with create_comfy_client(
            comfy_host, **comfy_client_kwargs
        ) as comfy_client:
            yield cls(
                comfy_client=comfy_client,
                batch_size=batch_size,
                input_base_dir=input_base_dir,
            )

    def submit_workflow(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        **kwargs,
    ) -> Generator[WorkflowOutputImage, None, None]:
        loop = asyncio.get_event_loop()
        yield from list(
            loop.run_until_complete(
                self.submit_workflow_async(
                    workflow_template=workflow_template,
                    input_images=input_images,
                    num_samples=num_samples,
                    randomize_seed=randomize_seed,
                    loop=loop,
                    **kwargs,
                )
            )
        )

    async def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        **kwargs,
    ) -> AsyncIterator[WorkflowOutputImage]:
        job_id = uuid.uuid4().hex

        uploads = [
            asyncio.create_task(
                self.comfy_client.upload_image(f"{i:04d}.jpg", image, subfolder=job_id)
            )
            for i, image in enumerate(input_images)
        ]

        self.logger.info(f"Uploading {len(uploads)} images for job {job_id}...")

        await asyncio.gather(*uploads)

        self.logger.info(f"Images uploaded for job {job_id}. Submitting workflow...")

        batch_size = kwargs.setdefault("batch_size", self.batch_size)
        
        input_images_dir = Path(self.input_base_dir) / job_id

        workflow = workflow_template.render(
            input_images_dir=str(input_images_dir),
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
                asyncio.create_task(self.comfy_client.enqueue_workflow(submit_workflow))
            )

        self.logger.info(f"Workflow submitted for job {job_id}. Waiting for results...")

        futures = [result.future for result in await asyncio.gather(*prompts)]

        for i, future in enumerate(futures):
            try:
                result = await future
            except Exception as e:
                if ignore_errors:
                    self.logger.error(
                        f"Got error for batch {i + 1}/{batch_count} of job {job_id}: {e}"
                    )
                    continue
                else:
                    raise

            self.logger.info(
                f"Got results for batch {i + 1}/{batch_count} of job {job_id}"
            )

            for image_item in result.output_images:
                yield WorkflowOutputImage(
                    image=image_item.image,
                    name=image_item.filename,
                    subfolder=None,
                )


class DummyWorkflowExecutor(BaseWorkflowExecutor):
    def __init__(
        self,
        image_folder: str | None = None,
        image_size: int = 512,
        batch_size: int = 1,
        fallback_fill_color: tuple[int, int, int] = (255, 255, 255),
    ):
        self.image_size = image_size
        self.batch_size = batch_size
        self.fallback_fill_color = fallback_fill_color

        if image_folder is not None:
            self.images = [
                ImageFactory.open(path) for path in utils.glob_images(image_folder)
            ]
        else:
            self.images = [self.create_dummy_image()]

    def create_dummy_image(self):
        image = ImageFactory.new("RGB", (self.image_size, self.image_size))
        image.paste(self.fallback_fill_color, (0, 0, image.width, image.height))
        return image

    def submit_workflow(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        **kwargs,
    ) -> Generator[WorkflowOutputImage, None, None]:
        for i in range(num_samples):
            yield WorkflowOutputImage(
                image=random.choice(self.images),
                name=f"{i:03d}.jpg",
                subfolder=None,
            )

    async def submit_workflow_async(
        self,
        workflow_template: WorkflowTemplate,
        input_images: list[Image],
        num_samples: int = 1,
        randomize_seed: bool = True,
        ignore_errors: bool = False,
        **kwargs,
    ) -> AsyncIterator[WorkflowOutputImage]:
        images = list(
            self.submit_workflow(
                workflow_template, input_images, num_samples, randomize_seed, **kwargs
            )
        )

        for image in images:
            yield image
