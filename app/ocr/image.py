"""Upload validation + normalization for vehicle-document photos.

Everything here operates on in-memory bytes only -- nothing is ever written
to disk. The uploaded file's own filename is never trusted for anything
beyond display (see checkout_routes.py, where it's only echoed back as
inert text, never used to open/read/write a path).
"""

import io

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")
_ALLOWED_PIL_FORMATS = ("JPEG", "PNG", "WEBP")

# A vehicle document set is a handful of photos (front/back of one document,
# or a couple of alternate shots) -- not a bulk-upload feature. Kept small
# and explicit rather than "generous": each extra file is one more OpenAI
# vision call (see app.ocr.provider), so this bounds both cost and the
# number of images a single request has to validate/normalize.
MAX_FILES_PER_RECOGNITION = 5

# Longest side an image is downscaled to before an API call -- keeps text
# legible while bounding request size/cost; images already smaller than
# this are left untouched (never upscaled/"enhanced").
_MAX_DIMENSION = 2000
_JPEG_QUALITY = 85


class UploadValidationError(Exception):
    """Raised with a message that is always safe to show the user
    directly -- never wraps provider/library internals."""


def _has_allowed_extension(filename: str | None) -> bool:
    if not filename:
        return False
    lowered = filename.lower()
    return any(lowered.endswith(ext) for ext in ALLOWED_EXTENSIONS)


def validate_and_normalize_upload(data: bytes, *, filename: str | None, content_type: str | None) -> bytes:
    """Validates extension + declared content-type + actual size, then
    decodes the bytes with Pillow to confirm they really are one of the
    allowed image formats (never trusting the extension/content-type
    alone -- both are attacker-controlled). Returns JPEG-encoded bytes,
    EXIF-orientation-corrected and downscaled if oversized, ready to hand
    to an OcrProvider. Raises UploadValidationError with a user-facing
    message on any failure; never returns partial/best-effort output.
    """
    if not data:
        raise UploadValidationError("Файл пустой. Загрузите фото документа.")

    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("Файл слишком большой. Максимальный размер — 10 МБ.")

    if not _has_allowed_extension(filename):
        raise UploadValidationError("Неподдерживаемый формат файла. Загрузите JPEG, PNG или WEBP.")

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadValidationError("Неподдерживаемый формат файла. Загрузите JPEG, PNG или WEBP.")

    try:
        image = Image.open(io.BytesIO(data))
        image.load()  # force full decode now -- catches truncated/corrupt/spoofed files
    except Exception as exc:
        raise UploadValidationError("Не удалось открыть файл как изображение. Попробуйте другое фото.") from exc

    if image.format not in _ALLOWED_PIL_FORMATS:
        raise UploadValidationError("Неподдерживаемый формат файла. Загрузите JPEG, PNG или WEBP.")

    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if max(image.size) > _MAX_DIMENSION:
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()
