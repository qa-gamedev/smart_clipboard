"""Локальный офлайн-перевод NLLB-200 (CTranslate2, int8): 200 языков, прямой перевод X → русский.

* Работает без интернета и не зависит от блокировок сервисов; быстро на обычном CPU.
* Модель (≈630 МБ, лицензия CC-BY-NC 4.0 — для личного использования) скачивается один раз
  кнопкой в настройках или командой  python install_model.py
* В память модель загружается только в момент перевода и выгружается через пару минут простоя.
* Безопасность: загрузка только по HTTPS с huggingface.co, проверка размера и SHA-256, файлы модели —
  это веса и словари (не исполняемый код и не pickle). Ничего из скачанного не запускается.
"""
from __future__ import annotations

import gc
import hashlib
import importlib.util
import logging
import os
import threading
from pathlib import Path

import settings as cfg

log = logging.getLogger(__name__)

MODEL_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
MODEL_FILES = ["config.json", "shared_vocabulary.txt", "sentencepiece.bpe.model", "model.bin"]
MODEL_DIR = cfg.CONFIG_DIR / "models" / "nllb-600m-int8"
BASE_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/"
MIN_SIZES = {"model.bin": 500_000_000, "sentencepiece.bpe.model": 1_000_000}
IDLE_UNLOAD_SEC = 150

FLORES = {
    "ru": "rus_Cyrl", "en": "eng_Latn", "uk": "ukr_Cyrl", "be": "bel_Cyrl", "bg": "bul_Cyrl", "sr": "srp_Cyrl",
    "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn", "nl": "nld_Latn",
    "pl": "pol_Latn", "cs": "ces_Latn", "tr": "tur_Latn", "sv": "swe_Latn", "fi": "fin_Latn", "da": "dan_Latn",
    "no": "nob_Latn", "hu": "hun_Latn", "ro": "ron_Latn", "vi": "vie_Latn", "id": "ind_Latn",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "zh-CN": "zho_Hans", "zh-TW": "zho_Hant", "zh": "zho_Hans",
    "ar": "arb_Arab", "iw": "heb_Hebr", "he": "heb_Hebr", "fa": "pes_Arab", "el": "ell_Grek", "th": "tha_Thai",
    "hi": "hin_Deva", "ka": "kat_Geor", "hy": "hye_Armn",
}


def nllb_code(code: str) -> str:
    return FLORES.get(code) or FLORES.get(code.split("-")[0]) or "eng_Latn"


# =====================================================================================
#  Состояние
# =====================================================================================
def deps_status() -> tuple[bool, str]:
    missing = [name for name in ("ctranslate2", "sentencepiece") if importlib.util.find_spec(name) is None]
    if missing:
        return False, "pip install " + " ".join(missing)
    return True, "установлены"


def _complete(name: str) -> bool:
    path = MODEL_DIR / name
    return path.is_file() and path.stat().st_size >= MIN_SIZES.get(name, 1)


def is_installed() -> bool:
    return all(_complete(n) for n in MODEL_FILES)


def status_text() -> str:
    ok, deps = deps_status()
    model = "скачана" if is_installed() else "не скачана"
    return f"модель {model}; пакеты ctranslate2/sentencepiece: {'установлены' if ok else 'нет (' + deps + ')'}"


def is_ready() -> bool:
    return is_installed() and deps_status()[0]


# =====================================================================================
#  Безопасная загрузка модели (докачка + проверка SHA-256)
# =====================================================================================
class DownloadCancelled(Exception):
    pass


_HDR = {"User-Agent": "SmartClipboard/1.0", "Accept-Encoding": "identity"}   # identity: Content-Length = реальный размер


def _total_from_range(value: str | None) -> int | None:
    """'bytes 0-0/12345' или 'bytes */12345' -> 12345."""
    if value and "/" in value:
        tail = value.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    return None


def _remote_info(name: str) -> tuple[int, str | None]:
    """(размер, sha256). ВАЖНО: Content-Length ответа-редиректа — это размер страницы-перенаправления,
    а не файла, поэтому размер берём только из надёжных заголовков."""
    import requests
    url = BASE_URL + name
    sha, size = None, 0
    r = requests.get(url, headers={**_HDR, "Range": "bytes=0-0"}, allow_redirects=False, stream=True, timeout=20)
    try:
        etag = (r.headers.get("X-Linked-Etag") or "").strip('"')
        if len(etag) == 64:
            sha = etag
        size = int(r.headers.get("X-Linked-Size") or 0)
        if not size and r.status_code in (200, 206):
            size = _total_from_range(r.headers.get("Content-Range")) or \
                (int(r.headers.get("Content-Length") or 0) if r.status_code == 200 else 0)
    finally:
        r.close()
    if not size:                                           # идём за редиректом и читаем Content-Range конечного ответа
        r = requests.get(url, headers={**_HDR, "Range": "bytes=0-0"}, allow_redirects=True, stream=True, timeout=20)
        try:
            size = _total_from_range(r.headers.get("Content-Range")) or \
                (int(r.headers.get("Content-Length") or 0) if r.status_code == 200 else 0)
        finally:
            r.close()
    return size, sha


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fetch(name: str, size: int, sha: str | None, base_done: int, total: int, progress, cancelled,
           _retry: bool = True) -> int:
    import requests
    dest, part = MODEL_DIR / name, MODEL_DIR / (name + ".part")
    pos = part.stat().st_size if part.exists() else 0
    if size and pos > size:                                   # битый хвост — начинаем заново
        part.unlink()
        pos = 0
    headers = {**_HDR, **({"Range": f"bytes={pos}-"} if pos else {})}
    with requests.get(BASE_URL + name, headers=headers, stream=True, timeout=(15, 40), allow_redirects=True) as r:
        if r.status_code == 416:
            # «Диапазон за пределами файла»: либо файл уже докачан, либо кусок не от этого файла.
            real = _total_from_range(r.headers.get("Content-Range"))
            if pos and ((real is not None and real == pos) or (size and size == pos)):
                pass                                          # докачано полностью — идём на проверку
            elif pos and _retry:
                part.unlink(missing_ok=True)
                return _fetch(name, size, sha, base_done, total, progress, cancelled, _retry=False)
            else:
                r.raise_for_status()
        else:
            r.raise_for_status()
            if pos and r.status_code != 206:                  # сервер не поддержал докачку — пишем с нуля
                pos = 0
            expected = int(r.headers.get("Content-Length") or 0)
            done = pos
            with open(part, "ab" if pos else "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if cancelled():
                        raise DownloadCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(base_done + done, total, name)
            if expected and done - pos != expected:
                raise OSError(f"{name}: соединение оборвалось — повторите загрузку (докачается)")
    final = part.stat().st_size
    if final == 0 or (size and final != size):
        part.unlink(missing_ok=True)
        raise OSError(f"{name}: получен неверный размер ({final}) — повторите загрузку")
    if sha:
        if progress:
            progress(base_done + final, total, f"проверка {name}")
        if _sha256_of(part) != sha:
            part.unlink(missing_ok=True)
            raise OSError(f"{name}: контрольная сумма не совпала — файл удалён, повторите загрузку")
    os.replace(part, dest)
    return final


def download_model(progress=None, cancelled=None) -> None:
    """progress(скачано_байт, всего_байт, имя_файла); cancelled() -> bool. Можно прерывать и продолжать."""
    cancelled = cancelled or (lambda: False)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    todo = [n for n in MODEL_FILES if not _complete(n)]
    info = {n: _remote_info(n) for n in todo}
    total = sum(size for size, _ in info.values())
    done = 0
    for name in todo:
        size, sha = info[name]
        done += _fetch(name, size, sha, done, total, progress, cancelled)
    if progress:
        progress(total, total, "готово")


# =====================================================================================
#  Движок
# =====================================================================================
class NllbEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._translator = None
        self._sp = None
        self._timer: threading.Timer | None = None

    def _load(self) -> None:
        import ctranslate2
        import sentencepiece
        threads = max(2, min(4, os.cpu_count() or 4))           # не занимаем все ядра
        self._translator = ctranslate2.Translator(str(MODEL_DIR), device="cpu", compute_type="int8",
                                                  inter_threads=1, intra_threads=threads)
        self._sp = sentencepiece.SentencePieceProcessor()
        self._sp.load(str(MODEL_DIR / "sentencepiece.bpe.model"))

    def _schedule_unload(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(IDLE_UNLOAD_SEC, self.unload)
        self._timer.daemon = True
        self._timer.start()

    def unload(self) -> None:
        with self._lock:
            self._translator, self._sp = None, None
        gc.collect()

    def translate_lines(self, lines: list[str], src_codes: list[str], target: str) -> list[str]:
        """Переводит список строк; src_codes — коды языков оригинала (ru, ko, en...)."""
        if not lines:
            return []
        if not is_ready():
            raise RuntimeError("офлайн-модель не установлена")
        tgt = nllb_code(target)
        with self._lock:
            if self._translator is None:
                self._load()
            sources = [[nllb_code(code)] + self._sp.encode(line, out_type=str) + ["</s>"]
                       for line, code in zip(lines, src_codes)]
            results = self._translator.translate_batch(
                sources, target_prefix=[[tgt]] * len(sources), beam_size=2,
                max_batch_size=24, max_decoding_length=256)
            out = []
            for res in results:
                tokens = res.hypotheses[0]
                if tokens and tokens[0] == tgt:
                    tokens = tokens[1:]
                out.append(self._sp.decode(tokens))
            self._schedule_unload()
            return out


ENGINE = NllbEngine()
