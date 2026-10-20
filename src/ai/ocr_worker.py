"""Subprocess entry point for one OCR page.

Running OCR in a child process lets the parent kill a wedged ONNX call
without losing the whole nightly run.
"""

from __future__ import annotations

import json
import sys

from src.ai.ocr import ocr_image_text


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m src.ai.ocr_worker INPUT OUTPUT", file=sys.stderr)
        return 2
    input_path, output_path = sys.argv[1], sys.argv[2]
    try:
        with open(input_path, "rb") as f:
            text = ocr_image_text(f.read())
        payload = {"text": text}
        code = 0
    except Exception as exc:  # noqa: BLE001 - returned to the parent
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        code = 1
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
