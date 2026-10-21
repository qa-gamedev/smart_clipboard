"""Калькулятор на лету для карточек: считает только числа и арифметику.
Никакого eval: выражение разбирается через ast и вычисляется белым списком операций."""
from __future__ import annotations

import ast
import operator as op
import re

MAX_LEN = 120
_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow}
_UN = {ast.UAdd: op.pos, ast.USub: op.neg}
_ALLOWED = re.compile(r"^[\d\s+\-*/().%]+$")
_DATE_LIKE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$")


def _prepare(text: str) -> str | None:
    t = text.strip().rstrip("=").strip()
    if not t or len(t) > MAX_LEN:
        return None
    for a, b in (("×", "*"), ("÷", "/"), ("−", "-"), ("–", "-"), ("\u00a0", " "), ("^", "**"), (":", "/")):
        t = t.replace(a, b)
    t = re.sub(r"(?<=\d)\s*[xхXХ]\s*(?=\d)", "*", t)                 # 3x4, 3 х 4
    if "." not in t and "," in t:
        # 1,000+5 → тысячи; 1,5*2 → десятичная запятая
        t = t.replace(",", "") if re.search(r"\d,\d{3}(?!\d)", t) else t.replace(",", ".")
    t = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)                    # «1 000 + 5»
    return t if _ALLOWED.match(t) else None


def _looks_like_math(t: str) -> bool:
    if not re.search(r"\d", t) or _DATE_LIKE.match(t):
        return False
    core = t.strip()
    return bool(re.search(r"[*/%]", core)                            # * / % **
                or re.search(r"(?<=[\d)])\s*\+\s*(?=[\d(])", core)   # 5+3
                or re.search(r"(?<=[\d)])\s+-\s+(?=[\d(])", core))   # «5 - 3» (с пробелами; «10-5» — скорее диапазон)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 1000 or (abs(a) > 10 and abs(b) > 100)):
            raise ValueError("слишком большая степень")
        return _BIN[type(node.op)](a, b)
    raise ValueError("недопустимое выражение")


def format_number(x: float | int) -> str:
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            raise ValueError("не число")
        if x.is_integer() and abs(x) < 1e15:
            return str(int(x))
        return f"{x:.10g}"
    return str(x)


def evaluate(text: str) -> str | None:
    """'150 * 24 / 1.2' -> '3000'. None — если это не выражение или его нельзя посчитать."""
    t = _prepare(text)
    if t is None or not _looks_like_math(t):
        return None
    try:
        return format_number(_eval(ast.parse(t, mode="eval")))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, RecursionError, MemoryError):
        return None
