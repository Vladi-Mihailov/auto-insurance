"""Upload validation for vehicle-document photos -- all synthetic,
in-memory images (see app/ocr/image.py). Never a real/downloaded document,
per the "no real personal documents in fixtures" constraint."""

import io

import pytest
from PIL import Image

from app.ocr.image import MAX_UPLOAD_BYTES, UploadValidationError, validate_and_normalize_upload


def _make_image_bytes(fmt="JPEG", size=(20, 20), color=(200, 50, 50)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def test_jpeg_accepted():
    result = validate_and_normalize_upload(_make_image_bytes("JPEG"), filename="doc.jpg", content_type="image/jpeg")
    Image.open(io.BytesIO(result)).load()  # still a valid, decodable image


def test_png_accepted():
    result = validate_and_normalize_upload(_make_image_bytes("PNG"), filename="doc.png", content_type="image/png")
    Image.open(io.BytesIO(result)).load()


def test_webp_accepted():
    result = validate_and_normalize_upload(_make_image_bytes("WEBP"), filename="doc.webp", content_type="image/webp")
    Image.open(io.BytesIO(result)).load()


def test_unsupported_extension_rejected():
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(_make_image_bytes("JPEG"), filename="doc.pdf", content_type="image/jpeg")


def test_unsupported_content_type_rejected_even_with_allowed_extension():
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(_make_image_bytes("JPEG"), filename="doc.jpg", content_type="application/pdf")


def test_non_image_bytes_with_spoofed_image_extension_and_content_type_rejected():
    """Extension + declared content-type are both attacker-controlled --
    the actual Pillow decode is what catches a spoofed file."""
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(b"this is not an image", filename="doc.jpg", content_type="image/jpeg")


def test_oversized_file_rejected():
    oversized = b"\xff" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(oversized, filename="doc.jpg", content_type="image/jpeg")


def test_empty_file_rejected():
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(b"", filename="doc.jpg", content_type="image/jpeg")


def test_path_traversal_shaped_filename_only_affects_the_extension_check():
    """validate_and_normalize_upload never opens/writes any path derived
    from the filename -- it only ever inspects its suffix."""
    result = validate_and_normalize_upload(
        _make_image_bytes("JPEG"), filename="../../../etc/passwd.jpg", content_type="image/jpeg"
    )
    Image.open(io.BytesIO(result)).load()


def test_missing_filename_rejected():
    with pytest.raises(UploadValidationError):
        validate_and_normalize_upload(_make_image_bytes("JPEG"), filename=None, content_type="image/jpeg")


def test_oversized_dimensions_are_downscaled_for_cost_control():
    result = validate_and_normalize_upload(
        _make_image_bytes("JPEG", size=(3000, 100)), filename="doc.jpg", content_type="image/jpeg"
    )
    resized = Image.open(io.BytesIO(result))
    assert max(resized.size) <= 2000


def test_small_image_is_not_upscaled():
    result = validate_and_normalize_upload(
        _make_image_bytes("JPEG", size=(20, 20)), filename="doc.jpg", content_type="image/jpeg"
    )
    resized = Image.open(io.BytesIO(result))
    assert resized.size == (20, 20)
