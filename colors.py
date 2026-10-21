"""Распознавание цветов в тексте: #rgb, #rrggbb, #rrggbbaa, rgb()/rgba(), hsl()/hsla()."""
from __future__ import annotations

import colorsys
import re

_HEX = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FUNC = re.compile(r"^(rgba?|hsla?)\(\s*([^)]*?)\s*\)$", re.IGNORECASE)


def _num(token: str, scale: float = 255.0) -> float:
    token = token.strip()
    return float(token[:-1]) * scale / 100 if token.endswith("%") else float(token)


def _frac(token: str) -> float:
    """'82%' -> 0.82; '0.82' -> 0.82; '82' -> 0.82."""
    v = float(token[:-1]) / 100 if token.endswith("%") else float(token)
    return max(0.0, min(1.0, v / 100 if v > 1 else v))


def _alpha(token: str | None) -> int:
    if token is None:
        return 255
    a = _num(token, 1.0)
    return max(0, min(255, round(a * 255)))


def parse_color(text: str) -> tuple[int, int, int, int] | None:
    """-> (r, g, b, a) в диапазоне 0..255 или None. Только если ВЕСЬ текст — цвет."""
    t = text.strip().rstrip(";").strip()
    if not t or len(t) > 40:
        return None
    m = _HEX.match(t)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return r, g, b, int(h[6:8], 16) if len(h) == 8 else 255
    m = _FUNC.match(t)
    if not m:
        return None
    kind = m.group(1).lower()
    parts = [p for p in re.split(r"[,\s/]+", m.group(2)) if p]
    try:
        if len(parts) not in (3, 4):
            return None
        a = _alpha(parts[3] if len(parts) == 4 else None)
        if kind.startswith("rgb"):
            r, g, b = (max(0, min(255, round(_num(p)))) for p in parts[:3])
            return r, g, b, a
        h = float(parts[0].rstrip("deg")) % 360 / 360
        s, l = (_frac(p) for p in parts[1:3])
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return round(r * 255), round(g * 255), round(b * 255), a
    except ValueError:
        return None


def to_hex(c: tuple[int, int, int, int]) -> str:
    r, g, b, a = c
    return f"#{r:02x}{g:02x}{b:02x}" + (f"{a:02x}" if a != 255 else "")


def to_rgb(c: tuple[int, int, int, int]) -> str:
    r, g, b, a = c
    return f"rgb({r}, {g}, {b})" if a == 255 else f"rgba({r}, {g}, {b}, {a / 255:.2f})"


def to_hsl(c: tuple[int, int, int, int]) -> str:
    r, g, b, a = c
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    body = f"{round(h * 360)}, {round(s * 100)}%, {round(l * 100)}%"
    return f"hsl({body})" if a == 255 else f"hsla({body}, {a / 255:.2f})"


FORMATS = (("HEX", to_hex), ("RGB", to_rgb), ("HSL", to_hsl))
