import json
import requests

from jinja2 import Template, StrictUndefined, meta


class WorkflowTemplate:
    REQUIRED_VARIABLES = ["input_images_dir", "batch_size"]

    def __init__(self, template_string: str):
        self.workflow_template = Template(template_string, undefined=StrictUndefined)

        ast = self.workflow_template.environment.parse(template_string)

        undeclared_variables = meta.find_undeclared_variables(ast)
        if undeclared_variables < set(self.REQUIRED_VARIABLES):
            raise ValueError(
                f"Missing variables in workflow template: {set(self.REQUIRED_VARIABLES) - undeclared_variables}"
            )

    @classmethod
    def from_file(cls, filename):
        with open(filename) as f:
            return cls(f.read())

    @classmethod
    def from_url(cls, url):
        response = requests.get(url)
        response.raise_for_status()
        return cls(response.text)

    def render(self, **kwargs):
        return json.loads(self.workflow_template.render(**kwargs))
