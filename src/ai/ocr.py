"""OCR using RapidOCR (ONNX, ~20MB total). Provides a simple sync API.

The RapidOCR runtime is thread-safe per-instance but model files are large,
so we load ONE recognizer per process and let multiple threads call it.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

from PIL import Image
from rapidocr_onnxruntime import RapidOCR

from src.runtime import config

_lock = threading.Lock()
_engine: RapidOCR | None = None
_timeout_state_lock = threading.Lock()
_consecutive_timeouts = 0
_breaker_reason: str | None = None


class OCRTimeoutError(TimeoutError):
    """A single OCR page exceeded the watchdog timeout."""


def _get_engine() -> RapidOCR:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = RapidOCR()
    return _engine


@dataclass
class OCRBlock:
    text: str
    confidence: float
    box: list


def ocr_image(image_bytes: bytes) -> list[OCRBlock]:
    """Run OCR on raw image bytes. Returns list of recognized blocks.

    Returns [] on any decode/engine failure (never raises for normal failures).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.width * img.height > config.OCR_MAX_PIXELS:
                print(
                    f"[OCR] image too large: {img.width}x{img.height} "
                    f"(limit {config.OCR_MAX_PIXELS} pixels)"
                )
                return []
            if img.mode != "RGB":
                img = img.convert("RGB")
            import numpy as np
            arr = np.array(img)
    except Exception as e:
        print(f"[OCR] image decode failed: {type(e).__name__}: {e}")
        return []

    engine = _get_engine()
    try:
        result, _elapsed = engine(arr)
    except Exception as e:
        print(f"[OCR] engine call failed: {type(e).__name__}: {e}")
        return []

    if not result:
        return []

    blocks = []
    for item in result:
        if len(item) < 3:
            continue
        box, text, score = item[0], item[1], float(item[2])
        if not text or not text.strip():
            continue
        blocks.append(OCRBlock(text=text.strip(), confidence=score, box=box))
    return blocks


def ocr_image_text(image_bytes: bytes) -> str:
    """Convenience: OCR an image and return all recognized text joined by newlines."""
    blocks = ocr_image(image_bytes)
    return "\n".join(b.text for b in blocks)


def ocr_image_text_with_timeout(
    image_bytes: bytes, timeout: float | None = None,
) -> str:
    """Run OCR with a watchdog and a consecutive-timeout circuit breaker.

    RapidOCR/ONNX can hang on rare pathological images.  The worker runs in
    a daemon thread so the caller can return and mark the page failed; after
    several consecutive hangs the remaining pages fail fast instead of
    spawning more stuck threads.
    """
    global _consecutive_timeouts, _breaker_reason
    timeout = timeout or config.OCR_TIMEOUT_SECONDS
    with _timeout_state_lock:
        if _breaker_reason:
            raise OCRTimeoutError(_breaker_reason)

    result: dict[str, object] = {}

    def _run():
        try:
            result["text"] = ocr_image_text(image_bytes)
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            result["error"] = exc

    thread = threading.Thread(
        target=_run, name="ocr-watchdog", daemon=True,
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with _timeout_state_lock:
            _consecutive_timeouts += 1
            if _consecutive_timeouts >= config.OCR_TIMEOUT_BREAKER:
                _breaker_reason = (
                    "OCR engine circuit breaker tripped after "
                    f"{_consecutive_timeouts} consecutive timeouts"
                )
        print(
            f"[OCR] page timed out after {timeout:.0f}s; "
            "marking it failed and continuing.",
            flush=True,
        )
        raise OCRTimeoutError(f"OCR timed out after {timeout:.0f}s")

    with _timeout_state_lock:
        _consecutive_timeouts = 0
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return str(result.get("text") or "")
