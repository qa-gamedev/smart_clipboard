"""Движки распознавания текста и объединение их результатов.

* Windows OCR  — встроенный, офлайн, всегда доступен (нужны языковые пакеты Windows).
* Tesseract    — необязательный, офлайн; читает сразу несколько языков в одном проходе
                 (русский + английский + корейский ...). Устанавливается отдельно.
* OCR.space    — необязательный онлайн-движок (бесплатный ключ); только для ручного
                 распознавания, в фоне интернет не используется.
Фоновое распознавание скриншотов (fast=True) всегда идёт только через Windows OCR —
минимальная нагрузка. Ручное (Alt+S, просмотр картинки) объединяет всё, что доступно,
и для каждой строки выбирает самый правдоподобный вариант.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage

import qr_scan
import winutil

log = logging.getLogger(__name__)

MAX_DIM = 2600          # предел Windows OCR по стороне изображения


class OcrError(Exception):
    pass


INSTALL_HINT = (
    "Windows OCR недоступен. Установите пакеты из requirements.txt и языковые пакеты OCR "
    "(запустите install_ocr_languages.ps1 от администратора)."
)


# =====================================================================================
#  Оценка и объединение результатов
# =====================================================================================
# Частые пары букв: у настоящего текста большинство пар «правдоподобны», у мусора
# (кириллица, прочитанная латинским движком, и наоборот) — почти нет.
_LAT_BI = set(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de "
    "hi ri ro ic ne ea ra ce li ch ll be ma si om ur ca el ta la ns di fo ho pe ec pr no ct us ac ot il tr "
    "ly nc et ut ss so rs un lo wa ge ie wh ee wi em ad ol rt po we na ul ni ts mo ow pa im mi ai sh ir su "
    "id os iv ia am fi ag ck ke ev ld ob qu ph ex ui up ab ap ei au ue ud".split())
_CYR_BI = set(
    "ст но ен то на ов ни ра во ко ре ро по ли ла ал ел ор пр ол од ть ка ер та ан ет те ий ны ти ат ва ог "
    "ос ле ом ем ит ес не за ль ди де ск об ми ие ня ся ой ве ур ик ин им ма ло до ча ых ое ей ут му ше ну "
    "ту ед ир ис ап ам ад ац еч ож ым их жд ги ри ее ют ую су ыв ыл ыт вы вт вс сл см со тр ус уж уд ум рт "
    "ру ры ря ях ял ят ёт ён ём ея ию ия юч чи че ци цы щи ши жи же ба бо бы бе бл бр гр го гл да дв др "
    "ду ез жа зн зы йс кр ку кл мо мн мы мя нг нн нс нт нц нь оз ок он оп от оч ощ".split())
_JUNK_CHARS = set("•¢£¥§¶¤¦·°©®™�")
_RUN = re.compile(r"[^\W\d_]+")
_DIGIT_IN_WORD = re.compile(r"[^\W\d_]\d+[^\W\d_]")      # W1ndows, Foundat10n


def _line_stats(text: str) -> tuple[float, int, float]:
    """(качество, число букв, штрафы)."""
    q, n, pen = 0.0, 0, 0.0
    for tok in text.split():
        for run in _RUN.findall(tok):
            size = len(run)
            n += size
            cyr = sum("\u0400" <= c <= "\u04ff" for c in run)
            lat = sum(c.isascii() or "\u00c0" <= c <= "\u024f" for c in run)
            if not cyr and not lat:                    # корейский, китайский, арабский...: проверить нечем
                q += size * 0.55
                continue
            plaus = 0.5
            if size > 1:
                bigrams = _CYR_BI if cyr >= lat else _LAT_BI
                low = run.lower()
                plaus = sum(low[i:i + 2] in bigrams for i in range(size - 1)) / (size - 1)
            q += size * (0.2 + 0.8 * plaus)
            if cyr and lat:                            # смесь алфавитов в одном слове — почти всегда ошибка
                pen += 2.5 * (min(cyr, lat) + 1)
        if _DIGIT_IN_WORD.search(tok):
            pen += 3.0
        pen += sum(c in _JUNK_CHARS for c in tok)
    return q, n, pen


def line_score(text: str) -> float:
    """Чем выше, тем правдоподобнее строка (суммарно)."""
    q, _n, pen = _line_stats(text)
    return q - pen


def merge_ocr_rows(sources: list[list[tuple]]) -> list[tuple]:
    """sources — результаты разных движков/вариантов, каждый: [(текст, x, y, w, h), ...].
    Строки сопоставляются по положению на картинке, для каждой берётся лучший вариант.
    Возвращает [(текст, x, y, w, h)] — по одной строке на ряд."""
    sources = [s for s in sources if s]
    if not sources:
        return []
    if len(sources) == 1:
        return [ln for ln in sources[0] if ln[0].strip()]
    items = [(t, x, y, w, h, si) for si, src in enumerate(sources) for (t, x, y, w, h) in src if t.strip()]
    items.sort(key=lambda it: it[2] + it[4] / 2)
    rows: list[list] = []                              # [верх, низ, элементы]
    for it in items:
        _t, _x, y, _w, h, _si = it
        for row in reversed(rows[-3:]):
            overlap = min(row[1], y + h) - max(row[0], y)
            if overlap >= 0.6 * min(row[1] - row[0], h):
                row[0], row[1] = min(row[0], y), max(row[1], y + h)
                row[2].append(it)
                break
        else:
            rows.append([y, y + h, [it]])
    out = []
    for _y0, _y1, its in rows:
        cands = []
        for si in sorted({i[5] for i in its}):
            mine = sorted((i for i in its if i[5] == si), key=lambda i: i[1])
            text = " ".join(i[0] for i in mine)
            x0, y0 = min(i[1] for i in mine), min(i[2] for i in mine)
            x1, y1 = max(i[1] + i[3] for i in mine), max(i[2] + i[4] for i in mine)
            cands.append((text, (x0, y0, x1 - x0, y1 - y0), *_line_stats(text)))
        n_max = max((c[3] for c in cands), default=0) or 1
        best, best_score = cands[0], float("-inf")
        for cand in cands:
            _text, _box, q, n, pen = cand
            mean = max(0.0, q - pen) / max(1, n)       # средняя «правдоподобность» буквы
            score = mean * (0.6 + 0.4 * n / n_max)     # неполные строки слегка штрафуем
            if score > best_score:
                best, best_score = cand, score
        out.append((best[0], *best[1]))
    return out


def merge_ocr_sources(sources: list[list[tuple]]) -> str:
    return "\n".join(t for t, *_ in merge_ocr_rows(sources)).strip()


def text_is_weak(text: str) -> bool:
    """Мало букв или они неправдоподобны — похоже, нужен движок другого языка."""
    q, n, pen = _line_stats(text)
    return n < 3 or max(0.0, q - pen) / n < 0.6


# =====================================================================================
#  Подготовка изображения
# =====================================================================================
def _mean_luma(gray: QImage) -> float:
    small = gray.scaled(96, 96, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    n = small.sizeInBytes()
    ptr = small.constBits()
    ptr.setsize(n)
    data = ptr.asstring(n)
    return sum(data) / max(1, len(data))


def prepare_variants(image: QImage, fast: bool) -> tuple[list[QImage], float]:
    """Увеличение мелкого текста, серый цвет, инверсия тёмного фона. Возвращает (1–2 варианта, масштаб).
    Все движки получают одни и те же варианты, поэтому координаты строк совпадают."""
    if image.isNull():
        raise OcrError("Пустое изображение")
    w, h = image.width(), image.height()
    side = max(w, h)
    k = min(3.0, MAX_DIM / side) if side < 1600 else min(1.0, MAX_DIM / side)
    if abs(k - 1.0) > 0.01:
        image = image.scaled(max(1, round(w * k)), max(1, round(h * k)),
                             Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    mean = _mean_luma(gray)
    if mean < 105:              # тёмная тема → инвертируем
        inverts = [True]
    elif mean > 150:
        inverts = [False]
    else:                       # неясно: пробуем оба варианта (в фоне — один)
        inverts = [mean < 128] if fast else [False, True]
    variants = []
    for inv in inverts:
        g = gray.copy()
        if inv:
            g.invertPixels()
        variants.append(g)
    return variants, k


def _encode(image: QImage, fmt: str, quality: int = -1) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, fmt, quality)
    return bytes(buf.data())


# =====================================================================================
#  Windows OCR (WinRT)
# =====================================================================================
class WinOcr:
    CAPS = {"fast": 2, "overlay": 3, "accurate": 8}      # сколько языковых движков использовать

    def __init__(self, settings) -> None:
        self.settings = settings
        # Один выделенный поток: WinRT/COM живёт в нём, приоритет понижен.
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="win-ocr", initializer=self._thread_init)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._m = None
        self._engines: dict = {}
        self._last_error: Exception | None = None

    @staticmethod
    def _thread_init() -> None:
        winutil.init_com_mta()
        winutil.lower_current_thread_priority()

    def reset(self) -> None:
        self._engines.clear()

    def run(self, variants: list[QImage], mode: str = "accurate", tag: str | None = None,
            timeout: float = 120) -> list[list[tuple]]:
        """mode: fast (фон) | overlay (наложение на экран) | accurate (ручное распознавание)."""
        packed = []
        for g in variants:
            rgba = g.convertToFormat(QImage.Format.Format_RGBA8888)
            n = rgba.sizeInBytes()
            ptr = rgba.constBits()
            ptr.setsize(n)
            packed.append((ptr.asstring(n), rgba.width(), rgba.height()))
        return self._pool.submit(self._recognize, packed, mode, tag or "auto").result(timeout)

    def languages(self) -> list[tuple[str, str]]:
        return self._pool.submit(self._languages).result(15)

    # ---------- работа в потоке OCR ----------
    def _modules(self):
        if self._m is not None:
            return self._m
        try:
            try:
                from winrt.windows.media.ocr import OcrEngine
                from winrt.windows.globalization import Language
                from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
                from winrt.windows.storage.streams import DataWriter
            except ImportError:
                from winsdk.windows.media.ocr import OcrEngine
                from winsdk.windows.globalization import Language
                from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
                from winsdk.windows.storage.streams import DataWriter
        except ImportError as e:
            raise OcrError(INSTALL_HINT) from e
        self._m = SimpleNamespace(OcrEngine=OcrEngine, Language=Language, BitmapPixelFormat=BitmapPixelFormat,
                                  SoftwareBitmap=SoftwareBitmap, DataWriter=DataWriter)
        return self._m

    @staticmethod
    def _priority(tag: str) -> int:
        order = ["ru", "en", "ko", "ja", "zh", "uk", "de", "fr", "es", "it", "pt", "pl", "tr", "ar"]
        primary = tag.split("-")[0].lower()
        return order.index(primary) if primary in order else len(order)

    def _engines_for(self, m, mode: str, tag: str) -> list:
        """Явный язык — один движок. «Авто» — все установленные языки (в фоне только два основных):
        один языковой движок путает чужие буквы, поэтому смешанный текст читается несколькими."""
        key = (tag, mode)
        if key in self._engines:
            return self._engines[key]
        engines = []
        if tag != "auto":
            engine = m.OcrEngine.try_create_from_language(m.Language(tag))
            if engine is not None:
                engines.append(engine)
        else:
            tags = sorted((lang.language_tag for lang in m.OcrEngine.available_recognizer_languages),
                          key=self._priority)
            chosen, seen = [], set()
            for t in tags:                              # по одному движку на язык (без en-US + en-GB)
                primary = t.split("-")[0].lower()
                if primary not in seen:
                    seen.add(primary)
                    chosen.append(t)
            for t in chosen[: self.CAPS.get(mode, 2)]:
                engine = m.OcrEngine.try_create_from_language(m.Language(t))
                if engine is not None:
                    engines.append(engine)
        if not engines:
            engine = m.OcrEngine.try_create_from_user_profile_languages()
            if engine is not None:
                engines.append(engine)
        if not engines:
            raise OcrError(INSTALL_HINT)
        self._engines[key] = engines
        return engines

    def _languages(self):
        m = self._modules()
        return [(lang.language_tag, lang.display_name) for lang in m.OcrEngine.available_recognizer_languages]

    def _recognize(self, packed, mode: str, tag: str) -> list[list[tuple]]:
        m = self._modules()
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(self._recognize_async(m, packed, mode, tag))

    async def _read(self, engines, bitmaps) -> list[list[tuple]]:
        """Все движки читают одновременно (WinRT распараллеливает по ядрам)."""
        async def one(engine, bitmap):
            try:
                return self._lines_of(await engine.recognize_async(bitmap))
            except Exception as e:  # noqa: BLE001
                self._last_error = e
                log.info("Движок Windows OCR не справился: %s", e)
                return None
        results = await asyncio.gather(*(one(e, b) for b in bitmaps for e in engines))
        return [r for r in results if r is not None]

    async def _recognize_async(self, m, packed, mode: str, tag: str) -> list[list[tuple]]:
        engines = self._engines_for(m, mode, tag)
        bitmaps = []
        for data, w, h in packed:
            writer = m.DataWriter()
            writer.write_bytes(data)
            bitmaps.append(m.SoftwareBitmap.create_copy_from_buffer(
                writer.detach_buffer(), m.BitmapPixelFormat.RGBA8, w, h))
        if mode == "accurate":
            # Сначала два основных языка; остальные подключаем, только если результат неубедительный.
            sources = await self._read(engines[:2], bitmaps)
            if len(engines) > 2 and text_is_weak(merge_ocr_sources(sources)):
                sources += await self._read(engines[2:], bitmaps)
        else:
            sources = await self._read(engines, bitmaps)
        if not sources:
            raise OcrError(f"Не удалось распознать изображение: {self._last_error}")
        return sources

    @staticmethod
    def _lines_of(result) -> list[tuple]:
        lines = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            rects = [wd.bounding_rect for wd in words]
            x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
            x1, y1 = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
            lines.append((line.text, x0, y0, x1 - x0, y1 - y0))
        return lines


# =====================================================================================
#  Tesseract (необязательный, офлайн)
# =====================================================================================
class TesseractOcr:
    DEFAULT_PATHS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
    PRIORITY = ["rus", "eng", "kor", "jpn", "chi_sim", "chi_tra", "ukr", "deu", "fra", "spa", "ita", "por",
                "pol", "tur", "ara", "bul", "ell", "heb", "tha", "hin"]

    def __init__(self, settings) -> None:
        self.settings = settings
        self._exe: str | None = None
        self._checked = False
        self._langs: list[str] | None = None

    def reset(self) -> None:
        self._exe, self._checked, self._langs = None, False, None

    @staticmethod
    def _run(cmd: list[str], timeout: float):
        flags = 0x08000000 | 0x00004000                      # без окна консоли + пониженный приоритет
        env = dict(os.environ, OMP_THREAD_LIMIT="2")         # не занимаем все ядра
        return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags, env=env)

    def exe(self) -> str | None:
        if not self._checked:
            self._checked = True
            for cand in (self.settings.get("tesseract_path"), shutil.which("tesseract"), *self.DEFAULT_PATHS):
                if cand and os.path.isfile(cand):
                    self._exe = cand
                    break
        return self._exe

    def installed_langs(self) -> list[str]:
        if self._langs is None:
            self._langs = []
            exe = self.exe()
            if exe:
                try:
                    r = self._run([exe, "--list-langs"], 20)
                    text = (r.stdout + r.stderr).decode("utf-8", "replace")
                    self._langs = [ln.strip() for ln in text.splitlines()
                                   if ln.strip() and not ln.startswith("List of") and ln.strip() != "osd"]
                except Exception:  # noqa: BLE001
                    log.info("Tesseract: не удалось получить список языков", exc_info=True)
        return self._langs

    def langs_arg(self) -> str:
        wish = (self.settings.get("tesseract_langs") or "auto").strip()
        if wish != "auto":
            return wish
        installed = self.installed_langs()
        ordered = [l for l in self.PRIORITY if l in installed] + [l for l in installed if l not in self.PRIORITY]
        return "+".join(ordered[:5])

    def status(self) -> str:
        exe = self.exe()
        if not exe:
            return "не найден (необязательно)"
        langs = self.installed_langs()
        return f"найден, языки: {', '.join(langs) if langs else 'не определены'}"

    def run(self, variants: list[QImage]) -> list[list[tuple]]:
        exe = self.exe()
        langs = self.langs_arg()
        if not exe or not langs:
            raise OcrError("Tesseract не установлен")
        sources = []
        for img in variants:
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                img.save(path, "PNG", 50)
                r = self._run([exe, path, "stdout", "-l", langs, "tsv"], 60)
                if r.returncode != 0:
                    raise OcrError(r.stderr.decode("utf-8", "replace").strip()[:120] or "ошибка Tesseract")
                sources.append(self._parse_tsv(r.stdout.decode("utf-8", "replace")))
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return sources

    @staticmethod
    def _parse_tsv(tsv: str) -> list[tuple]:
        lines: dict = {}
        for row in tsv.splitlines()[1:]:
            p = row.split("\t")
            if len(p) < 12 or p[0] != "5":                    # уровень 5 = слово
                continue
            try:
                conf = float(p[10])
                x, y, w, h = (int(v) for v in p[6:10])
            except ValueError:
                continue
            text = p[11].strip()
            if text and conf >= 20:
                lines.setdefault((p[2], p[3], p[4]), []).append((x, y, w, h, text))
        out = []
        for words in lines.values():
            words.sort(key=lambda t: t[0])
            x0, y0 = min(t[0] for t in words), min(t[1] for t in words)
            x1, y1 = max(t[0] + t[2] for t in words), max(t[1] + t[3] for t in words)
            out.append((" ".join(t[4] for t in words), x0, y0, x1 - x0, y1 - y0))
        out.sort(key=lambda t: (t[2], t[1]))
        return out


# =====================================================================================
#  OCR.space (необязательный, онлайн; нужен бесплатный ключ)
# =====================================================================================
class OcrSpace:
    URL = "https://api.ocr.space/parse/image"

    def __init__(self, settings) -> None:
        self.settings = settings

    def enabled(self) -> bool:
        return bool((self.settings.get("ocrspace_key") or "").strip())

    def run(self, variants: list[QImage]) -> list[list[tuple]]:
        import requests
        img = variants[0]                                     # в интернет уходит только один вариант
        data = b""
        for quality in (85, 65, 45):                          # бесплатный лимит файла — 1 МБ
            data = _encode(img, "JPEG", quality)
            if len(data) <= 1_000_000:
                break
        r = requests.post(self.URL, timeout=45, files={"file": ("image.jpg", data, "image/jpeg")},
                          data={"apikey": self.settings.get("ocrspace_key").strip(), "language": "auto",
                                "OCREngine": "2", "isOverlayRequired": "true", "scale": "true"})
        r.raise_for_status()
        j = r.json()
        if j.get("IsErroredOnProcessing"):
            msg = j.get("ErrorMessage")
            raise OcrError("OCR.space: " + (", ".join(msg) if isinstance(msg, list) else str(msg))[:120])
        lines = []
        for res in j.get("ParsedResults") or []:
            overlay = (res.get("TextOverlay") or {}).get("Lines") or []
            for ln in overlay:
                words = ln.get("Words") or []
                if not words:
                    continue
                x0, y0 = min(w["Left"] for w in words), min(w["Top"] for w in words)
                x1 = max(w["Left"] + w["Width"] for w in words)
                y1 = max(w["Top"] + w["Height"] for w in words)
                lines.append((ln.get("LineText") or " ".join(w["WordText"] for w in words),
                              x0, y0, x1 - x0, y1 - y0))
            if not overlay and res.get("ParsedText"):          # без координат: равномерно по высоте
                texts = [t for t in res["ParsedText"].splitlines() if t.strip()]
                step = img.height() / max(1, len(texts))
                lines += [(t, 0, i * step, img.width(), step) for i, t in enumerate(texts)]
        if not lines:
            raise OcrError("OCR.space: текст не найден")
        return [lines]


# =====================================================================================
#  Общий вход
# =====================================================================================
class OcrHub:
    """ctx.ocr: recognize_qimage(image, fast), languages(), reset(), last_used, tesseract_status()."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.win = WinOcr(settings)
        self.tess = TesseractOcr(settings)
        self.online = OcrSpace(settings)
        self.last_used: list[str] = []
        self.last_codes: list[tuple[str, str]] = []
        self._pool = ThreadPoolExecutor(3, thread_name_prefix="ocr-hub",
                                        initializer=winutil.lower_current_thread_priority)

    def reset(self) -> None:
        self.win.reset()
        self.tess.reset()

    def languages(self) -> list[tuple[str, str]]:
        return self.win.languages()

    def diagnose(self, image: QImage) -> list[dict]:
        """Каждый доступный движок отдельно: что распознал и за сколько секунд (окно «Проверка распознавания»)."""
        import time as _time
        variants, _k = prepare_variants(image, False)
        tag = self.settings.get("ocr_lang") or "auto"
        jobs = [("Windows OCR", lambda: self.win.run(variants, "accurate", tag))]
        if self.tess.exe() and self.tess.installed_langs():
            jobs.append(("Tesseract", lambda: self.tess.run(variants)))
        if self.online.enabled():
            jobs.append(("OCR.space", lambda: self.online.run(variants)))
        results = []
        for name, job in jobs:
            started = _time.time()
            try:
                text, error = merge_ocr_sources(job()), None
            except Exception as ex:  # noqa: BLE001
                text, error = "", str(ex)
            results.append({"engine": name, "text": text, "error": error, "seconds": _time.time() - started})
        return results

    def tesseract_status(self) -> str:
        return self.tess.status()

    def recognize_lines(self, image: QImage, timeout: float = 60) -> list[tuple]:
        """Для наложения перевода на экран: строки с координатами [(текст, x, y, w, h)] в пикселях изображения.
        Только Windows OCR, один вариант, язык — из настройки «язык текста на экране»."""
        variants, k = prepare_variants(image, True)
        tag = self.settings.get("overlay_ocr_lang") or "auto"
        rows = merge_ocr_rows(self.win.run(variants, "overlay", tag, timeout))
        return [(t, x / k, y / k, w / k, h / k) for t, x, y, w, h in rows]

    def recognize_qimage(self, image: QImage, fast: bool = False, timeout: float = 150) -> str:
        """fast=True — фон: только Windows OCR. Иначе: всё доступное, параллельно, лучшая строка из вариантов."""
        self.last_codes = []
        if not fast:                                          # QR/штрихкод на картинке — выдаём его содержимое
            codes = qr_scan.decode_codes(image)
            if codes:
                self.last_used, self.last_codes = ["QR"], codes
                return "\n".join(text for _kind, text in codes)
        variants, _k = prepare_variants(image, fast)
        tag = self.settings.get("ocr_lang") or "auto"
        tasks = {"Windows": lambda: self.win.run(variants, "fast" if fast else "accurate", tag)}
        if not fast:
            if self.tess.exe() and self.tess.installed_langs():
                tasks["Tesseract"] = lambda: self.tess.run(variants)
            if self.online.enabled():
                tasks["OCR.space"] = lambda: self.online.run(variants)
        futures = {name: self._pool.submit(fn) for name, fn in tasks.items()}
        sources, used, errors = [], [], []
        for name, fut in futures.items():
            try:
                res = fut.result(timeout)
                if res:
                    sources.extend(res)
                    used.append(name)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                log.info("OCR-движок %s не сработал: %s", name, e)
        if not sources:
            err = next((e for e in errors if isinstance(e, OcrError)), errors[0] if errors else None)
            raise err if isinstance(err, OcrError) else OcrError(f"Не удалось распознать изображение: {err}")
        self.last_used = used
        return merge_ocr_sources(sources)
