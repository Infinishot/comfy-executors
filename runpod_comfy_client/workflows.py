import json

from pathlib import Path
from jinja2 import Template, StrictUndefined, meta


class WorkflowTemplate:
    REQUIRED_VARIABLES = ["input_images_dir", "batch_size"]

    def __init__(self, filename):
        self.workflow_template_raw = Path(filename).read_text()
        self.workflow_template = Template(
            self.workflow_template_raw, undefined=StrictUndefined
        )

        ast = self.workflow_template.environment.parse(self.workflow_template_raw)

        undeclared_variables = meta.find_undeclared_variables(ast)
        if undeclared_variables < set(self.REQUIRED_VARIABLES):
            raise ValueError(
                f"Missing variables in workflow template: {set(self.REQUIRED_VARIABLES) - undeclared_variables}"
            )

    def render(self, **kwargs):
        return json.loads(self.workflow_template.render(**kwargs))
