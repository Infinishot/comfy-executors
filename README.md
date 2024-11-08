comfy-executors
===============

Run ComfyUI workflows conveniently on different execution backends, including local ComfyUI servers and serverless execution on [RunPod](https://www.runpod.io/) or [Modal](https://modal.com/). Supports workflow templating, asynchronous execution and transparent batching.

Usage
-----

### Installation

```bash
pip install comfy-executors
```

### Export ComfyUI workflow in API format

ComfyUI has two different workflow formats. The _standard_ format for which contains additional meta data for the UI and a reduced API version which corresponds to the actual execution graph that is send to the backend. Exporting workflows in API format has to be enabled in the developer settings first.

<details>
  <summary>API format export instructions</summary>

![](./assets/figure01.png)

Afterwards, a separate button for API format export should appear below the normal "Save" button:

![](./assets/figure02.png)
</details>

### Prepare workflow for use with comfy-executors

The ComfyUI does not support parameterization of workflows out-of-the-box, i.e. a submitted workflow (in JSON format) will be executed _as it is_. To allow flexible parameterization of workflows, e.g. replacing prompts and the random seeds, `comfy-executors` uses workflow templating via the Jinja templating engine.

To prepare a workflow for use with `comfy-executors`, first append the `.jinja` suffix to the filename, e.g. `workflow.json` becomes `workflow.json.jinja`. The nodes for loading input images and supplying the empty latent image need to be templated to obtain their value from the `input_images_dir` and `batch_size` variables respectively. This should generally as follows:


```json
{
    "1": {
        "inputs": {
            "directory": "{{ input_images_dir }}",
            "image_load_cap": 0,
            "start_index": 0,
            "load_always": false
    },
    "class_type": "LoadImagesFromDir",
        "_meta": {
            "title": "Load Reference Images"
        }
    },
    "2": {
        "inputs": {
            "width": 896,
            "height": 1152,
            {# Batch sizes need to be templated as it will be determined by comfy-executors #}
            "batch_size": {{ batch_size|int }}
        },
    "class_type": "EmptyLatentImage",
        "_meta": {
            "title": "Empty Latent Image"
        }
    },
}
```

Additional variables can, of course, be introduced as well which then have to be passed to the workflow rendering function.

### Submitting workflows

`comfy-executors` supports different execution backends, including local or remote ComfyUI servers and execution on serverless GPUs on [RunPod](https://www.runpod.io/) or [Modal](https://modal.com/).

The `RunpodComfyWorkflowExecutor` class provides functionality to submit a workflow template to RunPod and handle the results.

#### ComfyUI server

```python
from comfy_api_client import create_client
from comfy_executors import ComfyServerWorkflowExecutor, WorkflowTemplate

comfyui_server = "localhost:8188"

workflow_template = WorkflowTemplate.from_file("workflow.json.jinja")

async with ComfyServerWorkflowExecutor.create(
    comfyui_server,
    batch_size=8,
) as executor:
    async for item in executor.submit_workflow_async(
        workflow_template=workflow_template,
        num_samples=16
    ):
        print(item)
```

#### RunPod

Coming soon.

#### Modal

Coming soon.


A few notes on the example above:

* The executor can be configured to use a certain batch size. The value can also be overwritten by providing a `batch_size` to the `submit_workflow` method.
* A value for `num_samples` can be provided to generate approximately `num_samples / batch_size` batches within a single RunPod job. While multiple jobs could be submitted to achieve the same results, generating multiple batches within a single job will generally make better use of node results caching and thus improve efficiency and latency.
* The result is a list of `WorkflowOutputImage` objects. These provide the output image, the filename as set by ComfyUI and optionally the subfolder to which the image has been saved originally.
