"""Upload validation + normalization for vehicle-document photos.

Everything here operates on in-memory bytes only -- nothing is ever written
to disk. The uploaded file's own filename is never trusted for anything
beyond display (see checkout_routes.py, where it's only echoed back as
inert text, never used to open/read/write a path).
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from app.ocr.orientation import OrientationDetector, rotate_upright

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
# Decoded-size ceiling (~40 MP, e.g. 8000x5000) -- far above any phone photo,
# low enough that a small file claiming huge dimensions is rejected from its
# header before Pillow ever allocates the pixels.
MAX_PIXELS = 40_000_000
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

    return normalize_image_bytes(data).jpeg_bytes


@dataclass(frozen=True)
class NormalizedImage:
    jpeg_bytes: bytes
    # Degrees the image was rotated clockwise by orientation detection
    # (after EXIF transposition); 0 when upright or not detected.
    rotated_clockwise: int
    # "not_checked" (no detector), "confident" (detector sure; rotated_clockwise
    # says by how much, possibly 0), or "unknown" (detector ran, wasn't sure).
    orientation: str

    @property
    def orientation_uncertain(self) -> bool:
        return self.orientation != "confident"


def normalize_image_bytes(
    data: bytes,
    *,
    orientation_detector: OrientationDetector | None = None,
    min_side: int = 0,
) -> NormalizedImage:
    """The byte-level half of upload handling, for callers that have no
    trustworthy filename/content-type of their own (e.g. a Telegram photo):
    real-size limit, real decode (never trusting any declared type),
    pixel-count limit (checked from the header BEFORE the full decode, so a
    decompression bomb is never expanded), optional minimum side length,
    EXIF transposition, optional orientation correction, downscale, JPEG
    re-encode. Raises UploadValidationError with a user-facing message."""
    if not data:
        raise UploadValidationError("Файл пустой. Загрузите фото документа.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadValidationError("Файл слишком большой. Максимальный размер — 10 МБ.")

    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise UploadValidationError("Изображение слишком большое по размеру в пикселях. Загрузите фото поменьше.")
        image.load()  # force full decode now -- catches truncated/corrupt/spoofed files
    except UploadValidationError:
        raise
    except Exception as exc:
        raise UploadValidationError("Не удалось открыть файл как изображение. Попробуйте другое фото.") from exc

    if image.format not in _ALLOWED_PIL_FORMATS:
        raise UploadValidationError("Неподдерживаемый формат файла. Загрузите JPEG, PNG или WEBP.")

    image = ImageOps.exif_transpose(image)
    if min(image.size) < min_side:
        raise UploadValidationError("Фото слишком маленькое — текст не прочитать. Сфотографируйте документ крупнее.")
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    rotated = 0
    orientation = "not_checked"
    if orientation_detector is not None:
        guess = orientation_detector.detect(image)
        if guess is None:
            orientation = "unknown"
        else:
            orientation = "confident"
            rotated = guess.rotate_clockwise
            image = rotate_upright(image, rotated)

    if max(image.size) > _MAX_DIMENSION:
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)

    return NormalizedImage(jpeg_bytes=_encode_jpeg(image), rotated_clockwise=rotated, orientation=orientation)


def rotated_variants(jpeg_bytes: bytes) -> list[bytes]:
    """The same (already normalized) image turned 90/180/270 degrees -- for
    ONE extra recognition attempt when the orientation couldn't be
    determined locally (see app.telegram_bot.documents)."""
    image = Image.open(io.BytesIO(jpeg_bytes))
    image.load()
    return [_encode_jpeg(rotate_upright(image, degrees)) for degrees in (90, 180, 270)]


def _encode_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()
