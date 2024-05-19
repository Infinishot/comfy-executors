import inspect
import pytest

from PIL.Image import Image
from pathlib import Path

from comfy_executors.executors import DummyWorkflowExecutor, WorkflowOutputImage


@pytest.mark.asyncio
@pytest.mark.parametrize("image_folder", [None, "tests/fixtures/dummy-images"])
async def test_dummy_image_folder(image_folder):
    executor = DummyWorkflowExecutor(image_folder=image_folder)

    if image_folder:
        assert len(executor.images) == len(list(Path(image_folder).iterdir()))
    else:
        assert len(executor.images) == 1

    for func in [executor.submit_workflow, executor.submit_workflow_async]:
        outputs = func(None, None)

        if inspect.isasyncgen(outputs):
            outputs = [output async for output in outputs]

        for output in outputs:
            assert isinstance(output, WorkflowOutputImage)
            assert isinstance(output.image, Image)
