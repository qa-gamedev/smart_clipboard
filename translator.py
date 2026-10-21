"""Перевод с ЛЮБОГО языка на выбранный (по умолчанию русский).

Как это работает
* Текст режется на сегменты по языку/письменности (русский, английский, корейский...),
  поэтому картинка с несколькими языками переводится целиком, а куски, которые уже на
  целевом языке, не трогаются.
* Онлайн-сервисы (Google, Google-резерв, Yandex, Lingva, MyMemory) опрашиваются ПАРАЛЛЕЛЬНО —
  берётся первый ответ, поэтому один «мёртвый» сервис не тормозит перевод.
* Офлайн: локальная модель NLLB-200 (быстро, 200 языков, без блокировок), затем Argos.
  Режим «offline_first» ставит офлайн-модель на первое место.
* Сервис, ответивший «лимит/запрещено», уходит на паузу; результаты кэшируются.
Модуль не зависит от Qt и Windows.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from urllib.parse import quote

log = logging.getLogger(__name__)


class TranslationError(Exception):
    def __init__(self, message: str, details: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []        # [(сервис, причина)]


TARGET_LANGS = [("Русский", "ru"), ("English", "en"), ("Українська", "uk"), ("Deutsch", "de"),
                ("Français", "fr"), ("Español", "es"), ("Italiano", "it"), ("Português", "pt"),
                ("Polski", "pl"), ("Türkçe", "tr"), ("中文", "zh-CN"), ("日本語", "ja"),
                ("한국어", "ko"), ("العربية", "ar")]

LANG_RU = {
    "ru": "русский", "uk": "украинский", "be": "белорусский", "bg": "болгарский", "sr": "сербский",
    "en": "английский", "de": "немецкий", "fr": "французский", "es": "испанский", "it": "итальянский",
    "pt": "португальский", "nl": "нидерландский", "pl": "польский", "tr": "турецкий", "cs": "чешский",
    "ja": "японский", "ko": "корейский", "zh-CN": "китайский", "zh-TW": "китайский", "ar": "арабский",
    "iw": "иврит", "he": "иврит", "el": "греческий", "th": "тайский", "hi": "хинди", "ka": "грузинский",
    "hy": "армянский", "vi": "вьетнамский", "id": "индонезийский", "sv": "шведский", "fi": "финский",
    "da": "датский", "no": "норвежский", "hu": "венгерский", "ro": "румынский", "fa": "персидский",
}

# =====================================================================================
#  Определение языка без интернета (грубое, но не зависящее от сервисов)
# =====================================================================================
_STOP = {
    "en": "the and is of to in that it with for as was on are this be from or have an by not but what all were "
          "when your can there use which how their will other about out many then them these some would make",
    "de": "der die das und ist nicht ein eine mit für auf den dem zu von sich auch es sie wir ich aber wie wenn "
          "oder noch nur bei nach dass wird sind einen",
    "fr": "le la les des une est pas pour que qui dans sur avec ce il elle nous vous mais ou son sont cette plus "
          "par au aux être très fait",
    "es": "el la los las una es por para que con del se no en su al lo como más pero sus ya esta este muy sin "
          "sobre también",
    "it": "il lo la gli le un una è non per che con di del della sono come più ma anche questo nel suo alla",
    "pt": "os as um uma é não para que com do da em se por mais como mas foi são também seu sua isso você",
    "nl": "de het een en van ik te dat die in is niet op aan met als voor zijn er maar om ook dan nog bij",
    "pl": "nie na się do to że jest jak ale po co tak za od przez dla już tylko czy",
    "tr": "ve bir bu da de için ile ben sen ne var çok daha gibi ama kadar her olarak",
}
_STOP = {lang: set(words.split()) for lang, words in _STOP.items()}
_ACCENTS = {"de": "ßäöü", "fr": "çœàâêèéîôûù", "es": "ñ¿¡áíóú", "it": "àèìòù", "pt": "ãõç",
            "pl": "łżźćśąęń", "tr": "ğşıİ"}
_SCRIPT_RANGES = [(0x3040, 0x30FF, "ja"), (0xAC00, 0xD7AF, "ko"), (0x4E00, 0x9FFF, "zh-CN"),
                  (0x0600, 0x06FF, "ar"), (0x0590, 0x05FF, "iw"), (0x0370, 0x03FF, "el"),
                  (0x0E00, 0x0E7F, "th"), (0x0900, 0x097F, "hi"), (0x10A0, 0x10FF, "ka"),
                  (0x0530, 0x058F, "hy"), (0x0400, 0x04FF, "cyr")]
_NON_LATIN_GROUPS = {"ja", "ko", "zh-CN", "ar", "iw", "el", "th", "hi", "ka", "hy"}


def detect_language(text: str) -> str:
    """Код языка по письменности и частым словам."""
    counts: dict[str, int] = {}
    for c in text:
        if not c.isalpha():
            continue
        o = ord(c)
        script = next((name for lo, hi, name in _SCRIPT_RANGES if lo <= o <= hi), "lat")
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return "en"
    if counts.get("ja"):
        return "ja"                       # кана есть — японский (иероглифы бывают и в китайском)
    top = max(counts, key=counts.get)
    low = text.lower()
    if top == "cyr":
        if any(c in low for c in "іїєґ"):
            return "uk"
        if "ў" in low:
            return "be"
        if any(c in low for c in "ђћџљњ"):
            return "sr"
        if low.count("ъ") >= 2 and not any(c in low for c in "ыэё"):
            return "bg"
        return "ru"
    if top == "lat":
        words = [w for w in re.findall(r"[^\W\d_]+", low) if len(w) > 1]    # однобуквенные слова — шум OCR
        scores = {lang: sum(w in stop for w in words) + sum(low.count(c) for c in _ACCENTS.get(lang, ""))
                  for lang, stop in _STOP.items()}
        best = max(scores, key=scores.get)
        return best if scores[best] >= 2 else "en"
    return top


def _norm_lang(code: str | None) -> str | None:
    if not code:
        return None
    low = code.strip().lower()
    if low in ("zh", "zh-hans", "zh-cn"):
        return "zh-CN"
    if low in ("zh-hant", "zh-tw"):
        return "zh-TW"
    if low == "he":
        return "iw"
    if low in ("nb", "nn"):
        return "no"
    return low.split("-")[0].split("_")[0]


def _group_of(code: str) -> str:
    if code in ("ru", "uk", "be", "bg", "sr"):
        return "cyr"
    return code if code in _NON_LATIN_GROUPS else "lat"


def split_segments(text: str, target: str) -> list[dict]:
    """Режет текст на куски одного языка. skip=True — кусок уже на целевом языке (не отправляем)."""
    groups: list[list] = []
    lead: list[str] = []
    for line in text.splitlines():
        if not any(c.isalpha() for c in line):       # пустые/цифровые строки прикрепляем к соседям
            (groups[-1][1] if groups else lead).append(line)
            continue
        g = _group_of(detect_language(line))
        if groups and groups[-1][0] == g:
            groups[-1][1].append(line)
        else:
            groups.append([g, (lead + [line]) if not groups else [line]])
    segs = []
    for g, lines in groups:
        seg = "\n".join(lines)
        code = detect_language(seg)
        segs.append({"text": seg, "code": code, "skip": g != "lat" and _norm_lang(code) == target})
    return segs or [{"text": text, "code": "en", "skip": True}]


# =====================================================================================
#  Вспомогательное: нарезка, таймауты, повторы
# =====================================================================================
def _chunks(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    for line in text.splitlines():
        if len(line) <= limit:
            pieces.append(line)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            while len(sentence) > limit:
                pieces.append(sentence[:limit])
                sentence = sentence[limit:]
            pieces.append(sentence)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _flatten(texts: list[str], limit: int):
    flat, owner = [], []
    for i, t in enumerate(texts):
        for ch in _chunks(t, limit):
            flat.append(ch)
            owner.append(i)
    return flat, owner


def _unflatten(parts: list[str], owner: list[int], n: int) -> list[str]:
    out: list[list[str]] = [[] for _ in range(n)]
    for p, o in zip(parts, owner):
        out[o].append(p)
    return ["\n".join(x) for x in out]


def _run(fn, texts: list[str], limit: int, pause: float = 0.0):
    """fn(chunk, text_index) -> (перевод, язык_оригинала|None). Возвращает (переводы, языки)."""
    flat, owner = _flatten(texts, limit)
    parts, det, sent = [], [None] * len(texts), False
    for ch, o in zip(flat, owner):
        if not ch.strip():
            parts.append(ch)
            continue
        if pause and sent:
            time.sleep(pause)            # не превышаем лимиты запросов в секунду
        sent = True
        res, lang = fn(ch, o)
        if not res:
            raise TranslationError("пустой ответ")
        parts.append(res)
        det[o] = det[o] or lang
    return _unflatten(parts, owner, len(texts)), det


def _with_timeout(fn, timeout: float):
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name="translate")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("сервис не отвечает")
    if "e" in box:
        raise box["e"]
    return box.get("v")


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Accept-Language": "en-US,en;q=0.9",
}


def _requests():
    import requests
    return requests


def _svc_code(code: str, kind: str) -> str:
    """Коды языков у сервисов немного различаются."""
    if kind in ("yandex", "lingva"):
        return {"zh-CN": "zh", "zh-TW": "zh", "iw": "he"}.get(code, code)
    if kind == "mymemory":
        return {"iw": "he"}.get(code, code)
    if kind == "argos":
        return {"zh-CN": "zh", "zh-TW": "zt", "iw": "he"}.get(code, code)
    return code


# =====================================================================================
#  Сервисы: (texts, target, hints) -> (переводы, языки оригинала)
# =====================================================================================
def _eng_google(texts, target, hints):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target=target)
    return _run(lambda ch, i: (tr.translate(ch), None), texts, 4500, 0.3)


def _eng_google_gtx(texts, target, hints):
    req = _requests()

    def one(ch, i):
        r = req.get("https://translate.googleapis.com/translate_a/single", headers=_HEADERS, timeout=8,
                    params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": ch})
        r.raise_for_status()
        data = r.json()
        lang = data[2] if len(data) > 2 and isinstance(data[2], str) else None
        return "".join(p[0] for p in data[0] if p and p[0]), lang

    return _run(one, texts, 1500, 0.3)


def _eng_yandex(texts, target, hints):
    req = _requests()
    ucid = uuid.uuid4().hex
    tgt = _svc_code(target, "yandex")

    def one(ch, i):
        r = req.post("https://translate.yandex.net/api/v1/tr.json/translate",
                     params={"id": f"{ucid}-0-0", "srv": "android"},
                     data={"text": ch, "lang": tgt}, headers=_HEADERS, timeout=8)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 200:
            raise TranslationError(str(j.get("message") or f"код {j.get('code')}"))
        return "\n".join(j.get("text") or []), (j.get("lang", "").split("-")[0] or None)

    return _run(one, texts, 3000, 0.1)


LINGVA_INSTANCES = ["https://lingva.ml", "https://lingva.thedaviddelta.com"]


def _eng_lingva(texts, target, hints):
    """Lingva — прокси к Google: запросы к Google идут с сервера Lingva, а не с вашего IP."""
    req = _requests()
    tgt = _svc_code(target, "lingva")
    last: Exception | None = None
    for base in LINGVA_INSTANCES:
        def one(ch, i, base=base):
            r = req.get(f"{base}/api/v1/auto/{tgt}/{quote(ch, safe='')}", headers=_HEADERS, timeout=6)
            r.raise_for_status()
            j = r.json()
            return j["translation"], (j.get("info") or {}).get("detectedSource")
        try:
            return _run(one, texts, 900, 0.2)
        except Exception as e:  # noqa: BLE001
            last = e
    raise last or TranslationError("нет доступных серверов")


def _eng_mymemory(texts, target, hints):
    """MyMemory не умеет 'auto' — нужен явный код языка оригинала (берём из подсказки)."""
    req = _requests()
    tgt = _svc_code(target, "mymemory")

    def one(ch, i):
        src = _svc_code(hints[i] or detect_language(ch), "mymemory")
        if src == tgt:
            return ch, src
        r = req.get("https://api.mymemory.translated.net/get", headers=_HEADERS, timeout=8,
                    params={"q": ch, "langpair": f"{src}|{tgt}"})
        r.raise_for_status()
        j = r.json()
        text = (j.get("responseData") or {}).get("translatedText") or ""
        if int(j.get("responseStatus", 200)) != 200 or "MYMEMORY WARNING" in text.upper() or not text.strip():
            raise TranslationError(text[:90] or f"код {j.get('responseStatus')}")
        return text, src

    return _run(one, texts, 160, 0.3)


# ---------- офлайн ----------
def _flatten_lines(texts: list[str], limit: int = 350):
    """Для локальной модели: каждая строка переводится отдельно (структура строк сохраняется)."""
    flat, owner = [], []
    for i, t in enumerate(texts):
        for line in t.split("\n"):
            parts = [line] if len(line) <= limit else _chunks(line, limit)
            for p in parts:
                flat.append(p)
                owner.append(i)
    return flat, owner


def _line_source(line: str, hint: str) -> str:
    code = detect_language(line)
    if _group_of(code) == "lat" and _group_of(hint) == "lat":
        return hint                       # у латиницы язык по одной строке определяется плохо — берём по сегменту
    return code


def _eng_nllb(texts, target, hints):
    """Локальная модель NLLB-200: без интернета, быстро, 200 языков, прямой перевод без английского «моста»."""
    import local_translate as lt
    if not lt.is_installed():
        raise TranslationError("офлайн-модель не скачана")
    ok, deps = lt.deps_status()
    if not ok:
        raise TranslationError(f"не хватает пакетов: {deps}")
    flat, owner = _flatten_lines(texts)
    todo, srcs = [], []
    for i, line in enumerate(flat):
        if not any(c.isalpha() for c in line):
            continue
        src = _line_source(line, hints[owner[i]])
        if _group_of(src) != "lat" and _norm_lang(src) == target:
            continue                      # строка уже на целевом языке
        todo.append(i)
        srcs.append(src)
    out = list(flat)
    if todo:
        for i, translated in zip(todo, lt.ENGINE.translate_lines([flat[i] for i in todo], srcs, target)):
            out[i] = translated
    det: list = [None] * len(texts)
    for i, src in zip(todo, srcs):
        det[owner[i]] = det[owner[i]] or src
    return _unflatten(out, owner, len(texts)), det


def _eng_argos(texts, target, hints):
    """Запасной офлайн-вариант (необязательно: pip install argostranslate; python install_offline.py ...)."""
    try:
        import argostranslate.translate as at
    except ImportError as e:
        raise TranslationError("Argos не установлен") from e
    langs = {lang.code: lang for lang in at.get_installed_languages()}
    tgt_lang = langs.get(_svc_code(target, "argos"))
    out, det = [], []
    for text, hint in zip(texts, hints):
        src = _svc_code(hint, "argos")
        src_lang = langs.get(src)
        if src_lang is None or tgt_lang is None:
            raise TranslationError(f"нет офлайн-модели {src}→{_svc_code(target, 'argos')}")
        tr = src_lang.get_translation(tgt_lang)
        if tr is None:
            raise TranslationError(f"нет офлайн-модели {src}→{_svc_code(target, 'argos')}")
        out.append(tr.translate(text))
        det.append(hint)
    return out, det


ONLINE = [("Google", _eng_google), ("Google (резерв)", _eng_google_gtx), ("Yandex", _eng_yandex),
          ("Lingva", _eng_lingva), ("MyMemory", _eng_mymemory)]
LOCAL = [("Локальная модель (NLLB)", _eng_nllb), ("Офлайн (Argos)", _eng_argos)]

# =====================================================================================
#  Каскад: гонка онлайн-сервисов, офлайн-модель, режимы, кэш
# =====================================================================================
_cooldown: dict[str, float] = {}
_state: dict = {"last_ok": None}
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
CACHE_MAX = 2000


def _short(e: Exception) -> str:
    raw = str(e).strip()
    s = re.sub(r"https?://\S+", "", raw).strip().splitlines()[0] if raw else type(e).__name__
    low = s.lower()
    if "429" in low or "too many" in low:
        return "лимит запросов (ваш IP временно ограничен)"
    if "401" in low or "unauthorized" in low:
        return "отказано в доступе (401)"
    if "403" in low or "forbidden" in low:
        return "доступ запрещён (403)"
    if "404" in low:
        return "сервис не найден (404)"
    if "timed out" in low or "timeout" in low or "не отвечает" in low:
        return "нет ответа (таймаут)"
    if "connection" in low or "resolve" in low or "max retries" in low:
        return "нет соединения"
    return s[:90]


def _penalty_seconds(e: Exception) -> int:
    s = str(e).lower()
    blocked = any(x in s for x in ("429", "too many", "401", "403", "404", "unauthorized", "forbidden", "quota"))
    return 600 if blocked else 60


def reset_cooldowns() -> None:
    _cooldown.clear()


def _race(engines, texts, target, hints, timeout, details):
    """Запускает сервисы параллельно и берёт первый успешный ответ — не ждём медленные и мёртвые."""
    q: queue.Queue = queue.Queue()

    def worker(name, fn):
        try:
            parts, det = fn(texts, target, hints)
            if parts and any(p and p.strip() for p in parts):
                q.put((name, parts, det, None))
            else:
                q.put((name, None, None, TranslationError("пустой результат")))
        except Exception as e:  # noqa: BLE001
            q.put((name, None, None, e))

    for name, fn in engines:
        threading.Thread(target=worker, args=(name, fn), daemon=True, name=f"tr-{name}").start()
    pending = {name for name, _ in engines}
    deadline = time.time() + timeout
    while pending:
        try:
            name, parts, det, err = q.get(timeout=max(0.05, deadline - time.time()))
        except queue.Empty:
            break
        pending.discard(name)
        if err is None:
            _state["last_ok"] = name
            _cooldown.pop(name, None)
            return parts, name, det
        details.append((name, _short(err)))
        _cooldown[name] = time.time() + _penalty_seconds(err)
        log.info("Переводчик %s недоступен: %s", name, err)
    for name in pending:                                           # не успели ответить
        details.append((name, "нет ответа (таймаут)"))
        _cooldown[name] = time.time() + 60
    return None


def _run_online(texts, target, hints, details, force):
    now = time.time()
    ready = [e for e in ONLINE if _cooldown.get(e[0], 0) <= now]
    if not ready:
        if not force:
            details.append(("Онлайн", "все сервисы на паузе после ошибок"))
            return None
        ready = list(ONLINE)                                       # ручной запрос — пробуем всё равно
    last = _state["last_ok"]
    phases = [[e for e in ready if e[0] == last], [e for e in ready if e[0] != last]]
    for phase in phases:                                            # сначала последний рабочий, потом остальные
        if phase:
            result = _race(phase, texts, target, hints, 12, details)
            if result:
                return result
    return None


def _run_local(texts, target, hints, details):
    for name, fn in LOCAL:
        try:
            parts, det = fn(texts, target, hints)
            if parts and any(p and p.strip() for p in parts):
                return parts, name, det
            raise TranslationError("пустой результат")
        except Exception as e:  # noqa: BLE001
            details.append((name, _short(e)))
            log.info("Офлайн-переводчик %s: %s", name, e)
    return None


def _cascade(texts, target, hints, mode="auto", force_online=True):
    details: list[tuple[str, str]] = []
    steps = ([lambda: _run_local(texts, target, hints, details),
              lambda: _run_online(texts, target, hints, details, force_online)] if mode == "offline_first" else
             [lambda: _run_online(texts, target, hints, details, force_online),
              lambda: _run_local(texts, target, hints, details)])
    for step in steps:
        result = step()
        if result:
            return result
    raise TranslationError("все сервисы перевода недоступны", details)


def _translate_batch(texts, target, hints, mode, force_online):
    """Кэш + каскад. Повторяющиеся строки (субтитры, интерфейс игры) не переводятся заново."""
    out: list = [None] * len(texts)
    det: list = [None] * len(texts)
    misses = []
    with _cache_lock:
        for i, t in enumerate(texts):
            hit = _cache.get((target, t))
            if hit is not None:
                out[i], det[i] = hit
                _cache.move_to_end((target, t))
            else:
                misses.append(i)
    engine = "кэш"
    if misses:
        parts, engine, d = _cascade([texts[i] for i in misses], target, [hints[i] for i in misses],
                                    mode, force_online)
        with _cache_lock:
            for k, i in enumerate(misses):
                out[i], det[i] = parts[k], d[k]
                _cache[(target, texts[i])] = (parts[k], d[k])
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    return out, engine, det


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def translate(text: str, target: str = "ru", mode: str = "auto") -> tuple[str, str, list[str]]:
    """Возвращает (перевод, сервис, языки оригинала). Сервис '—' = перевод не потребовался."""
    target = _norm_lang(target) or "ru"
    if target == "zh":
        target = "zh-CN"
    segs = split_segments(text, target)
    todo = [i for i, s in enumerate(segs) if not s["skip"]]
    if not todo:
        return text, "—", [s["code"] for s in segs]
    parts, engine, det = _translate_batch([segs[i]["text"] for i in todo], target,
                                          [segs[i]["code"] for i in todo], mode, True)
    langs: list[str] = []
    for k, i in enumerate(todo):
        segs[i]["out"] = parts[k]
        code = _norm_lang(det[k]) or segs[i]["code"]
        if code and code not in langs:
            langs.append(code)
    return "\n".join(s.get("out", s["text"]) for s in segs), engine, langs


def translate_lines(lines: list[str], target: str = "ru", mode: str = "auto") -> list[str]:
    """Построчный перевод для наложения на экран: возвращает список той же длины.
    Строки на целевом языке и «пустые» остаются как есть. Не долбит сервисы, находящиеся на паузе."""
    target = _norm_lang(target) or "ru"
    if target == "zh":
        target = "zh-CN"
    out = list(lines)
    todo, hints = [], []
    for i, line in enumerate(lines):
        if sum(c.isalpha() for c in line) < 2:
            continue
        code = detect_language(line)
        if _group_of(code) != "lat" and _norm_lang(code) == target:
            continue
        todo.append(i)
        hints.append(code)
    if todo:
        parts, _engine, _det = _translate_batch([lines[i] for i in todo], target, hints, mode, False)
        for i, p in zip(todo, parts):
            out[i] = p
    return out
