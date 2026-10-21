"""Настройки приложения.

Хранятся в %APPDATA%\\ClipboardManager\\settings.json, а не в папке данных:
корневая папка хранения сама является настройкой.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "ClipboardManager"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "settings.json"

DEFAULTS = {
    "root": "",                    # папка хранения (пусто = по умолчанию)
    "hotkey_history": "Alt+V",
    "hotkey_ocr": "Alt+S",
    "autostart": False,
    "cleanup_days": 60,            # 0 = не удалять
    "background_ocr": True,        # OCR скриншотов в фоне
    "ocr_lang": "auto",            # "auto" = языки профиля Windows, либо тег: ru, en-US...
    "translate_to": "ru",          # язык перевода по умолчанию
    "tesseract_path": "",          # необязательно; пусто = искать автоматически
    "tesseract_langs": "auto",     # например "rus+eng+kor"
    "ocrspace_key": "",            # необязательно: бесплатный ключ ocr.space (онлайн-OCR)
    "plain_text": False,
    "pinned_visible": 3,           # сколько закреплённых показывать наверху (вне «Избранного»)
    "merge_mode": "newline",       # как склеивать несколько выбранных карточек
    "convert_currency": "RUB",     # в какую валюту переводить суммы из распознанного текста
    "import_history": True,        # при запуске подтянуть историю буфера Windows (Win+V)
    "note_hotkey": "Alt+N",        # быстрая новая заметка
    "translate_mode": "auto",      # auto = сначала онлайн, потом офлайн; offline_first = сначала офлайн-модель
    "auto_translate": False,       # переводить сразу после распознавания (окно Alt+S)
    # --- секретный режим (отдельное изолированное хранилище) ---
    "secret_enabled": False,
    "secret_pin": "",              # соленый pbkdf2-хеш, пусто = без PIN
    "secret_hotkey": "Alt+Shift+V",
    "secret_timeout_min": 10,      # автовыход из секретного режима при бездействии, 0 = выкл
    # --- перевод поверх экрана ---
    "overlay_hotkey": "Alt+T",
    "overlay_interval_ms": 1200,
    "overlay_ocr_lang": "auto",    # язык текста на экране: auto или тег (ko-KR, ja-JP, en-US...)
}


def hash_pin(pin: str) -> str:
    """PIN хранится только в виде соленого PBKDF2-хеша."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _kind, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), 200_000)
        return hmac.compare_digest(test.hex(), digest)
    except Exception:
        return False


def default_root() -> str:
    """Диск E:, если он есть, иначе папка в профиле пользователя."""
    if os.path.exists("E:\\"):
        return str(Path("E:\\") / APP_NAME)
    return str(Path.home() / APP_NAME)


class Settings:
    def __init__(self) -> None:
        self._d = dict(DEFAULTS)
        self.load()
        if not self._d["root"]:
            self._d["root"] = default_root()

    def load(self) -> None:
        try:
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            self._d.update({k: v for k, v in data.items() if k in DEFAULTS})
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("Не удалось прочитать настройки, используются значения по умолчанию", exc_info=True)

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._d, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            log.warning("Не удалось сохранить настройки", exc_info=True)

    def get(self, key: str):
        return self._d.get(key, DEFAULTS.get(key))

    def set(self, key: str, value, save: bool = True) -> None:
        self._d[key] = value
        if save:
            self.save()

    def update(self, values: dict) -> None:
        self._d.update({k: v for k, v in values.items() if k in DEFAULTS})
        self.save()
