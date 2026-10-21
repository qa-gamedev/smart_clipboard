"""Конвертер валют и величин для распознанного/переведённого текста.

* Валюты → рубли (или другая валюта из настроек) по курсу ЦБ РФ. Курс кэшируется на 6 часов и на диске:
  без интернета берётся последний сохранённый курс (с пометкой «устарел»).
* Величины → метрическая система: мили, футы, дюймы, ярды, фунты, унции, галлоны, °F, миль/ч.
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET

import settings as cfg

log = logging.getLogger(__name__)

CBR_XML = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_JSON = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_FILE = cfg.CONFIG_DIR / "rates.json"
MAX_AGE = 6 * 3600

SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₩": "KRW", "₺": "TRY", "₴": "UAH",
           "₸": "KZT", "₹": "INR", "₽": "RUB"}
CUR_SIGN = {"RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "KZT": "₸", "UAH": "₴", "TRY": "₺"}
_WORDS = [(r"^dollars?$", "USD"), (r"^долл\w*$", "USD"), (r"^bucks$", "USD"), (r"^euros?$", "EUR"), (r"^евро$", "EUR"),
          (r"^pounds?\s+sterling$", "GBP"), (r"^фунт\w*\s+стерлинг\w*$", "GBP"), (r"^yuan$", "CNY"), (r"^юан\w*$", "CNY"),
          (r"^rmb$", "CNY"), (r"^yen$", "JPY"), (r"^иен\w*$", "JPY"), (r"^гривн\w*$", "UAH"), (r"^грн$", "UAH"),
          (r"^тенге$", "KZT"), (r"^злот\w*$", "PLN"), (r"^zł$", "PLN"), (r"^руб\w*$", "RUB"), (r"^рупи\w*$", "INR"),
          (r"^франк\w*$", "CHF"), (r"^лир\w*$", "TRY")]

NUM = r"(?:\d{1,3}(?:[ \u00a0,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
_SYM = "[$€£¥₩₺₴₸₹₽]"
_CUR_RE = re.compile(
    rf"(?P<sym1>{_SYM})\s?(?P<num1>{NUM})"
    rf"|\b(?P<code2>[A-Z]{{3}})\s?(?P<num2>{NUM})(?![\d])"
    rf"|(?P<num3>{NUM})\s?(?P<tok3>{_SYM}|[A-Za-zА-Яа-яЁёł]+(?:\s+[A-Za-zА-Яа-яЁё]+)?)")

# единицы: (регулярное выражение, ключ)
_UNIT_DEFS = [(r"miles?|миль|мили|миля", "mile"), (r"mph", "mph"), (r"feet|foot|ft|фут\w*", "foot"),
              (r"inch(?:es)?|дюйм\w*", "inch"), (r"yards?|yd|ярд\w*", "yard"),
              (r"pounds?|lbs?|фунт\w*", "pound"), (r"ounces?|oz|унци\w*", "ounce"),
              (r"gallons?|gal|галлон\w*", "gallon"), (r"°\s?F|degrees?\s+F|fahrenheit", "fahr")]
# ключ: (множитель, единица, число знаков не нужно)
_UNIT_TO = {"mile": (1.609344, "км"), "mph": (1.609344, "км/ч"), "foot": (0.3048, "м"), "inch": (2.54, "см"),
            "yard": (0.9144, "м"), "pound": (0.45359237, "кг"), "ounce": (28.349523, "г"),
            "gallon": (3.785411784, "л")}
_UNIT_RE = re.compile(
    rf"(?P<num>{NUM})\s?(?P<unit>(?:{'|'.join(f'(?:{p})' for p, _ in _UNIT_DEFS)})(?![A-Za-zА-Яа-я])|[\"″])",
    re.IGNORECASE)


def parse_number(raw: str) -> float:
    s = raw.replace("\u00a0", " ").strip()
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace(" ", "").replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(" ", "")
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) and s.count(",") == 1 else s.replace(",", "")
    elif "." in s:
        s = s.replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):             # 1.500.000 — разделители тысяч
            s = s.replace(".", "")
    else:
        s = s.replace(" ", "")
    return float(s)


# ---------- курсы ЦБ ----------
_mem: dict = {}


def _parse_xml(content: bytes) -> dict:
    root = ET.fromstring(content)
    rates = {"RUB": 1.0}
    for v in root.findall("Valute"):
        code = v.findtext("CharCode")
        nominal = float((v.findtext("Nominal") or "1").replace(",", "."))
        value = float((v.findtext("Value") or "0").replace(",", "."))
        if code and value:
            rates[code] = value / nominal
    return {"date": root.get("Date") or "", "rates": rates}


def _parse_json(data: dict) -> dict:
    rates = {"RUB": 1.0}
    for code, v in (data.get("Valute") or {}).items():
        rates[code] = float(v["Value"]) / float(v.get("Nominal") or 1)
    d = (data.get("Date") or "")[:10]
    date = f"{d[8:10]}.{d[5:7]}.{d[0:4]}" if len(d) == 10 else ""
    return {"date": date, "rates": rates}


def get_rates(max_age: float = MAX_AGE) -> dict | None:
    """{'date': '16.10.2026', 'rates': {'USD': 92.1, ...}, 'stale': bool} или None."""
    now = time.time()
    if _mem and now - _mem.get("fetched", 0) < max_age:
        return _mem
    cached = None
    try:
        cached = json.loads(CACHE_FILE.read_text("utf-8"))
        if now - cached.get("fetched", 0) < max_age:
            _mem.update(cached, stale=False)
            return _mem
    except Exception:
        cached = None
    fresh = None
    try:
        import requests
        try:
            fresh = _parse_xml(requests.get(CBR_XML, timeout=6).content)
        except Exception:
            fresh = _parse_json(requests.get(CBR_JSON, timeout=6).json())
    except Exception as e:  # noqa: BLE001
        log.info("Курсы ЦБ недоступны: %s", e)
    if fresh and len(fresh["rates"]) > 3:
        fresh["fetched"] = now
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(fresh, ensure_ascii=False), "utf-8")
        except OSError:
            pass
        _mem.clear()
        _mem.update(fresh, stale=False)
        return _mem
    if cached and cached.get("rates"):                        # нет сети — последний сохранённый курс
        _mem.clear()
        _mem.update(cached, stale=True)
        return _mem
    return None


# ---------- поиск в тексте ----------
def _currency_code(token: str, rates: dict | None) -> str | None:
    tok = token.strip().rstrip(".")
    if tok in SYMBOLS:
        return SYMBOLS[tok]
    if re.fullmatch(r"[A-Z]{3}", tok) and (rates is None or tok in rates):
        return tok
    low = tok.lower()
    for pattern, code in _WORDS:
        if re.match(pattern, low):
            return code
    return None


def find_quantities(text: str, rates: dict | None = None) -> list[dict]:
    """[{'kind': 'currency'|'unit', 'value': float, 'code'/'unit': ..., 'src': '150 USD'}]"""
    found, seen = [], set()
    for m in _CUR_RE.finditer(text):
        num = m.group("num1") or m.group("num2") or m.group("num3")
        token = m.group("sym1") or m.group("code2") or m.group("tok3")
        code = _currency_code(token, rates)
        if not code:
            if m.group("tok3") and " " in m.group("tok3"):    # «150 people here»: пробуем только первое слово
                code = _currency_code(m.group("tok3").split()[0], rates)
            if not code:
                continue
        try:
            value = parse_number(num)
        except ValueError:
            continue
        key = ("c", code, value)
        if key not in seen:
            seen.add(key)
            found.append({"kind": "currency", "value": value, "code": code, "src": m.group(0).strip()})
    for m in _UNIT_RE.finditer(text):
        unit = m.group("unit")
        if unit in ('"', "″") and m.start("unit") != m.end("num"):
            continue                                          # кавычка — дюймы, только вплотную к числу
        tail = text[m.end():m.end() + 12].lower().lstrip()
        key_name = next((k for p, k in _UNIT_DEFS if re.fullmatch(p, unit, re.IGNORECASE)), "inch")
        if key_name == "pound" and tail.startswith(("стерл", "sterl")):
            continue                                          # фунты стерлингов — валюта
        try:
            value = parse_number(m.group("num"))
        except ValueError:
            continue
        key = ("u", key_name, value)
        if key not in seen:
            seen.add(key)
            found.append({"kind": "unit", "value": value, "unit": key_name, "src": m.group(0).strip()})
    return found


# ---------- форматирование ----------
def _fmt(x: float) -> str:
    if abs(x) >= 100:
        s = f"{x:,.0f}"
    elif abs(x) >= 10:
        s = f"{x:,.1f}"
    else:
        s = f"{x:,.2f}"
    s = s.replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else (s[:-2] if s.endswith(",0") else s)


def _amount(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def convert(q: dict, rates: dict | None, target: str = "RUB") -> str | None:
    if q["kind"] == "unit":
        if q["unit"] == "fahr":
            return f"{_fmt(q['value'])} °F ≈ {_fmt((q['value'] - 32) * 5 / 9)} °C"
        factor, name = _UNIT_TO[q["unit"]]
        return f"{q['src']} ≈ {_fmt(q['value'] * factor)} {name}"
    code = q["code"]
    if code == target or not rates or code not in rates or target not in rates:
        return None
    value = q["value"] * rates[code] / rates[target]
    return f"{_amount(q['value'])} {code} ≈ {_fmt(value)} {CUR_SIGN.get(target, target)}"


def describe(text: str, target: str = "RUB") -> list[str]:
    """Строки для панели «Конвертер»; пусто — нечего конвертировать. Вызывать в фоновом потоке."""
    info = get_rates() if any(q["kind"] == "currency" for q in find_quantities(text)) else None
    rates = info["rates"] if info else None
    quantities = find_quantities(text, rates)
    lines: list[str] = []
    for q in quantities:
        line = convert(q, rates, target)
        if line and line not in lines:
            lines.append(line)
    if any(q["kind"] == "currency" and q["code"] != target for q in quantities):
        if info:
            lines.append(f"Курс ЦБ РФ на {info.get('date') or '—'}" + (" (устарел: нет сети)" if info.get("stale") else ""))
        else:
            lines.append("Курс валют недоступен (нет сети и сохранённого курса)")
    return lines
