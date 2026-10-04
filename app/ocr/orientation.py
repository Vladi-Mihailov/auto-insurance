"""Optional, local, free orientation detection for document photos.

Telegram re-encodes "photo" uploads and drops EXIF, so a document shot
sideways or upside down arrives with that rotation baked into the pixels --
EXIF transposition (app.ocr.image) can't fix it, and a vision model reads a
90/180/270-degree VIN noticeably worse. An OrientationDetector looks at
the pixels and says how far to rotate the image to make it upright.

Everything here is OPTIONAL: the only concrete detector shells out to a
locally installed Tesseract binary (its OSD mode, no Python package needed).
When Tesseract isn't installed, isn't configured, times out, or isn't
confident, detection simply returns None and the image is left as it is --
the application never requires Tesseract to run, and never guesses a
rotation it isn't confident about.
"""

import io
import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger(__name__)

# Tesseract reports ~3.5-4.5 for a clean, correctly-detected document page
# (measured on rendered 0/90/180/270 samples); below this the call is
# treated as "not sure", never acted on.
DEFAULT_MIN_CONFIDENCE = 2.0
_TIMEOUT_SECONDS = 15
# OSD needs legible text, not a full-resolution photo -- a bounded copy
# keeps the local call fast.
_OSD_MAX_SIDE = 1600

_ROTATE_RE = re.compile(r"Rotate:\s*(\d+)")
_CONFIDENCE_RE = re.compile(r"Orientation confidence:\s*([\d.]+)")


@dataclass(frozen=True)
class OrientationGuess:
    # Degrees to rotate CLOCKWISE to make the image upright: 0/90/180/270.
    rotate_clockwise: int
    confidence: float


class OrientationDetector(ABC):
    name: str = "abstract"

    @abstractmethod
    def detect(self, image: Image.Image) -> OrientationGuess | None:
        """A confident guess, or None when unavailable/unsure. Never raises."""


class NullOrientationDetector(OrientationDetector):
    name = "none"

    def detect(self, image: Image.Image) -> OrientationGuess | None:
        return None


class TesseractOrientationDetector(OrientationDetector):
    """Runs `tesseract stdin stdout --psm 0` (orientation/script detection)."""

    name = "tesseract"

    def __init__(self, command: str, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE):
        self._command = command
        self._min_confidence = min_confidence

    def detect(self, image: Image.Image) -> OrientationGuess | None:
        try:
            sample = image.convert("L")
            if max(sample.size) > _OSD_MAX_SIDE:
                sample.thumbnail((_OSD_MAX_SIDE, _OSD_MAX_SIDE))
            buffer = io.BytesIO()
            sample.save(buffer, format="PNG")
            completed = subprocess.run(
                [self._command, "stdin", "stdout", "--psm", "0"],
                input=buffer.getvalue(),
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("Orientation detection unavailable (%s)", type(exc).__name__)
            return None
        if completed.returncode != 0:
            # Typical for low-text images ("Too few characters") -- not an error.
            return None
        output = completed.stdout.decode("utf-8", errors="ignore")
        rotate = _ROTATE_RE.search(output)
        confidence = _CONFIDENCE_RE.search(output)
        if not rotate or not confidence:
            return None
        degrees = int(rotate.group(1)) % 360
        score = float(confidence.group(1))
        if degrees not in (0, 90, 180, 270) or score < self._min_confidence:
            return None
        return OrientationGuess(rotate_clockwise=degrees, confidence=score)


def build_orientation_detector(mode: str | None, tesseract_command: str | None = None) -> OrientationDetector:
    """mode: "off" -> never detect; "auto" (default) -> Tesseract if a
    binary can be found, else none; "tesseract" -> same as auto, but logs
    a warning when it can't be found. Never raises."""
    mode = (mode or "auto").strip().lower()
    if mode == "off":
        return NullOrientationDetector()
    command = tesseract_command or shutil.which("tesseract")
    if not command:
        if mode == "tesseract":
            logger.warning("OCR_ORIENTATION_DETECTOR=tesseract but no tesseract binary was found; continuing without it")
        return NullOrientationDetector()
    return TesseractOrientationDetector(command)


def rotate_upright(image: Image.Image, rotate_clockwise: int) -> Image.Image:
    """PIL's rotate() is counter-clockwise, hence the negative angle."""
    if rotate_clockwise % 360 == 0:
        return image
    return image.rotate(-rotate_clockwise, expand=True)
