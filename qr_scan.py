"""Распознавание QR-кодов и штрихкодов на изображении (zxing-cpp). Необязательно:
без пакетов zxing-cpp и numpy просто возвращает пустой список."""
from __future__ import annotations

import importlib.util
import logging

log = logging.getLogger(__name__)


def available() -> bool:
    return importlib.util.find_spec("zxingcpp") is not None and importlib.util.find_spec("numpy") is not None


def _gray_array(image):
    import numpy as np
    from PyQt6.QtGui import QImage
    g = image.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h, bpl = g.width(), g.height(), g.bytesPerLine()
    ptr = g.constBits()
    ptr.setsize(bpl * h)
    arr = np.frombuffer(ptr.asstring(bpl * h), dtype=np.uint8).reshape(h, bpl)[:, :w]
    return np.ascontiguousarray(arr)


def decode_codes(image) -> list[tuple[str, str]]:
    """[(тип, текст)] — QR, EAN, Code128 и другие. Пробуем как есть, инвертированно и с увеличением."""
    if not available() or image is None or image.isNull():
        return []
    try:
        import numpy as np
        import zxingcpp
        arr = _gray_array(image)
        attempts = [arr, (255 - arr).astype(np.uint8)]
        if max(arr.shape) < 500:
            attempts.append(arr.repeat(2, axis=0).repeat(2, axis=1))
        for candidate in attempts:
            found, seen = [], set()
            for r in zxingcpp.read_barcodes(candidate):
                text = (r.text or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    found.append((str(r.format).split(".")[-1], text))
            if found:
                return found
    except Exception:  # noqa: BLE001
        log.info("Распознавание QR не удалось", exc_info=True)
    return []
