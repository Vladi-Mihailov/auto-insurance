"""Rotated-document preprocessing (app.ocr.orientation + app.ocr.image).

Pipeline logic is tested with fake detectors (always runs). The real
Tesseract check renders its own synthetic document text and is skipped when
no tesseract binary is installed -- Tesseract is optional by design."""

import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.ocr import orientation as orientation_module
from app.ocr.image import MAX_PIXELS, UploadValidationError, normalize_image_bytes, rotated_variants
from app.ocr.orientation import (
    NullOrientationDetector,
    OrientationDetector,
    OrientationGuess,
    TesseractOrientationDetector,
    build_orientation_detector,
    rotate_upright,
)


def _marked_image(size=(400, 300)) -> Image.Image:
    """White image with a red square in the top-left corner (orientation marker)."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).rectangle([0, 0, 40, 40], fill="red")
    return image


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _top_left_is_red(data: bytes) -> bool:
    r, g, b = Image.open(io.BytesIO(data)).convert("RGB").getpixel((10, 10))
    return r > 200 and g < 80 and b < 80


class FixedDetector(OrientationDetector):
    name = "fixed"

    def __init__(self, guess):
        self.guess = guess
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        return self.guess


# ------------------------------------------------------------ rotation math


@pytest.mark.parametrize("ccw", [90, 180, 270])
def test_rotate_upright_undoes_a_counter_clockwise_rotation(ccw):
    original = _marked_image()
    rotated = original.rotate(ccw, expand=True)  # what a sideways photo looks like
    restored = rotate_upright(rotated, ccw)  # Tesseract reports "Rotate: <ccw>"
    assert restored.size == original.size
    assert _top_left_is_red(_png(restored))


@pytest.mark.parametrize("ccw", [90, 180, 270])
def test_confident_detector_turns_the_photo_upright(ccw):
    detector = FixedDetector(OrientationGuess(rotate_clockwise=ccw, confidence=4.0))
    result = normalize_image_bytes(_png(_marked_image().rotate(ccw, expand=True)), orientation_detector=detector)
    assert detector.calls == 1
    assert (result.rotated_clockwise, result.orientation, result.orientation_uncertain) == (ccw, "confident", False)
    assert Image.open(io.BytesIO(result.jpeg_bytes)).size == (400, 300)
    assert _top_left_is_red(result.jpeg_bytes)


def test_unsure_detector_leaves_the_image_alone_and_says_so():
    rotated = _marked_image().rotate(90, expand=True)
    result = normalize_image_bytes(_png(rotated), orientation_detector=FixedDetector(None))
    assert (result.rotated_clockwise, result.orientation, result.orientation_uncertain) == (0, "unknown", True)
    assert Image.open(io.BytesIO(result.jpeg_bytes)).size == rotated.size


def test_without_detector_pipeline_still_works():
    result = normalize_image_bytes(_png(_marked_image()))
    assert (result.rotated_clockwise, result.orientation, result.orientation_uncertain) == (0, "not_checked", True)
    assert _top_left_is_red(result.jpeg_bytes)


def test_exif_orientation_is_applied_before_detection():
    image = _marked_image()
    upright_rotated = image.rotate(90, expand=True)  # stored sideways ...
    exif = Image.Exif()
    exif[0x0112] = 6  # ... with EXIF "rotate 90 CW to display"
    buffer = io.BytesIO()
    upright_rotated.save(buffer, format="JPEG", exif=exif, quality=95)
    detector = FixedDetector(OrientationGuess(rotate_clockwise=0, confidence=4.0))
    result = normalize_image_bytes(buffer.getvalue(), orientation_detector=detector)
    assert Image.open(io.BytesIO(result.jpeg_bytes)).size == (400, 300)
    assert _top_left_is_red(result.jpeg_bytes)


def test_rotated_variants_are_the_three_other_orientations():
    variants = rotated_variants(_jpeg(_marked_image()))
    assert [Image.open(io.BytesIO(v)).size for v in variants] == [(300, 400), (400, 300), (300, 400)]


# ---------------------------------------------------------- input limits


def test_malformed_bytes_rejected():
    with pytest.raises(UploadValidationError):
        normalize_image_bytes(b"\xff\xd8\xff not really a jpeg")


def test_unsupported_real_format_rejected():
    buffer = io.BytesIO()
    _marked_image().save(buffer, format="GIF")
    with pytest.raises(UploadValidationError):
        normalize_image_bytes(buffer.getvalue())


def test_pixel_bomb_rejected_from_header_before_decoding():
    huge = Image.new("1", (8000, 6000))  # 48 MP -- tiny as a PNG file
    assert 8000 * 6000 > MAX_PIXELS
    with pytest.raises(UploadValidationError, match="пикселях"):
        normalize_image_bytes(_png(huge))


def test_too_small_photo_rejected_when_minimum_requested():
    with pytest.raises(UploadValidationError, match="маленькое"):
        normalize_image_bytes(_png(_marked_image((200, 150))), min_side=300)
    normalize_image_bytes(_png(_marked_image((200, 150))))  # web path: no minimum, unchanged


# -------------------------------------------------- optional Tesseract


def test_missing_tesseract_binary_is_never_fatal():
    detector = TesseractOrientationDetector("definitely-not-an-installed-tesseract-binary")
    assert detector.detect(_marked_image()) is None


def test_builder_modes(monkeypatch):
    monkeypatch.setattr(orientation_module.shutil, "which", lambda name: None)
    assert isinstance(build_orientation_detector("off"), NullOrientationDetector)
    assert isinstance(build_orientation_detector("auto"), NullOrientationDetector)
    assert isinstance(build_orientation_detector("tesseract"), NullOrientationDetector)
    assert isinstance(build_orientation_detector("auto", "C:/tools/tesseract.exe"), TesseractOrientationDetector)
    monkeypatch.setattr(orientation_module.shutil, "which", lambda name: "/usr/bin/tesseract")
    assert isinstance(build_orientation_detector(None), TesseractOrientationDetector)
    assert isinstance(build_orientation_detector("off"), NullOrientationDetector)


def _fake_run(stdout: str, returncode: int = 0):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout.encode(), stderr=b"")

    return run


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("Page number: 0\nOrientation in degrees: 270\nRotate: 90\nOrientation confidence: 4.48\n", 0, 90),
        ("Rotate: 180\r\nOrientation confidence: 3.79\r\n", 0, 180),
        ("Rotate: 90\nOrientation confidence: 0.61\n", 0, None),  # not confident
        ("Too few characters. Skipping this page\n", 1, None),
        ("garbage", 0, None),
    ],
)
def test_tesseract_output_parsing(monkeypatch, stdout, returncode, expected):
    monkeypatch.setattr(orientation_module.subprocess, "run", _fake_run(stdout, returncode))
    guess = TesseractOrientationDetector("tesseract").detect(_marked_image())
    assert (guess.rotate_clockwise if guess else None) == expected


def test_tesseract_timeout_is_not_fatal(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=15)

    monkeypatch.setattr(orientation_module.subprocess, "run", slow)
    assert TesseractOrientationDetector("tesseract").detect(_marked_image()) is None


def _document_image() -> Image.Image:
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 34)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 34)
        except OSError:
            pytest.skip("no TrueType font available to render a document")
    image = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "CERTIFICATE OF VEHICLE REGISTRATION",
        "Registration number: AB123CD",
        "VIN: WVWZZZ1JZXW000001",
        "Make: VOLKSWAGEN   Model: GOLF",
        "Owner: IVANOV IVAN",
        "Colour: BLACK   Year: 2019",
        "Issued by the traffic police department",
    ]
    for index, line in enumerate(lines):
        draw.text((60, 60 + index * 110), line, fill="black", font=font)
    return image


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="optional: tesseract binary not installed")
@pytest.mark.parametrize("ccw", [0, 90, 180, 270])
def test_real_tesseract_detects_rotated_documents(ccw):
    detector = build_orientation_detector("auto")
    assert isinstance(detector, TesseractOrientationDetector)
    document = _document_image()
    result = normalize_image_bytes(_png(document.rotate(ccw, expand=True)), orientation_detector=detector)
    assert result.orientation == "confident"
    assert result.rotated_clockwise == ccw
    assert Image.open(io.BytesIO(result.jpeg_bytes)).size[0] > Image.open(io.BytesIO(result.jpeg_bytes)).size[1]
