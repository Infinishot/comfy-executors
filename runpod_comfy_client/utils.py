import io
import base64

from PIL import Image as ImageFactory
from PIL.Image import Image


def image_to_buffer(image: Image, format="jpeg"):
    buffer = io.BytesIO()
    image.save(buffer, format=format)
    buffer.seek(0)
    return buffer


def image_to_b64(image: Image, format="jpeg"):
    buffer = image_to_buffer(image, format=format)
    image_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
    return image_b64


def image_from_b64(image_b64: str):
    image_bytes = base64.b64decode(image_b64)
    image = ImageFactory.open(io.BytesIO(image_bytes))
    return image


def fullname(o):
    module = o.__class__.__module__
    if module is None or module == str.__class__.__module__:
        return o.__class__.__name__
    return module + "." + o.__class__.__name__
