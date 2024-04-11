import logging
from functools import cached_property
from comfy_executors import utils


class LoggingMixin:
    @cached_property
    def logger(self):
        return logging.getLogger(utils.fullname(self))
