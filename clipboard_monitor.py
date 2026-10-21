"""Мониторинг буфера обмена.

Никакого опроса по таймеру: используется сигнал QClipboard.dataChanged
(системное уведомление Windows), поэтому в простое нагрузка нулевая.
Тяжёлые операции (хеш/сохранение PNG/миниатюра/OCR) идут в фоновом потоке
с пониженным приоритетом.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage

import winutil
from database import detect_kind

log = logging.getLogger(__name__)

MAX_TEXT = 1_000_000     # символов; больше — обрезаем
MAX_HTML = 300_000       # форматированную версию длиннее не храним
THUMB_SIZE = (280, 180)


def make_thumbnail(db, image: QImage, rel_path: str) -> None:
    """Миниатюра для карточки — чтобы не декодировать огромные PNG при каждом открытии списка."""
    try:
        thumb = image.scaled(*THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        path = db.thumb_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        thumb.convertToFormat(QImage.Format.Format_RGB32).save(str(path), "JPEG", 82)
    except Exception:
        log.debug("Не удалось создать миниатюру", exc_info=True)


class ClipboardMonitor(QObject):
    changed = pyqtSignal()   # в истории что-то добавилось/обновилось

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.paused = False
        self._ignore_until = 0.0
        self._ocr_warned = False
        self._cb = QGuiApplication.clipboard()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)   # дребезг: dataChanged часто приходит пачкой
        self._timer.timeout.connect(lambda: self._process(0))
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="clip-worker",
                                        initializer=winutil.lower_current_thread_priority)
        self._cb.dataChanged.connect(self._on_changed)

    # ---------- API ----------
    def ignore_next(self, seconds: float = 0.8) -> None:
        """Вызывать перед записью в буфер из самой программы, чтобы не плодить записи."""
        self._ignore_until = time.monotonic() + seconds

    # ---------- что было скопировано ДО запуска ----------
    def scan_now(self) -> None:
        """Сохраняет то, что лежит в буфере сейчас (скопировано до запуска). Дубли отсекаются по хешу."""
        self._process(0)

    def import_history(self) -> None:
        """Подтягивает историю буфера Windows (Win+V), если она включена. Работает в фоне, повторы пропускает."""
        def job():
            try:
                import startup_import
                items = startup_import.read_history()
                db = self.ctx.db
                added = 0
                for kind, payload, ts in items:               # от старых к новым
                    if kind == "text":
                        added += db.add_text(payload[:MAX_TEXT], ts=ts, bump=False)[1]
                    else:
                        img = QImage.fromData(payload)
                        if not img.isNull():
                            self._pool.submit(self._save_image, db, img, ts)
                log.info("Импорт истории Windows: найдено %s, добавлено текстов: %s", len(items), added)
                if items:
                    self.changed.emit()
            except Exception:
                log.info("Импорт истории Windows не удался", exc_info=True)
        threading.Thread(target=job, daemon=True, name="history-import").start()

    # ---------- обработка ----------
    def _on_changed(self) -> None:
        if self.paused or time.monotonic() < self._ignore_until:
            return
        self._timer.start()

    @staticmethod
    def _is_protected(mime) -> bool:
        """Менеджеры паролей помечают данные, которые нельзя сохранять в историю."""
        for fmt in mime.formats():
            if "ExcludeClipboardContentFromMonitorProcessing" in fmt:
                return True
            if "CanIncludeInClipboardHistory" in fmt:
                if bytes(mime.data(fmt))[:4] == b"\x00\x00\x00\x00":
                    return True
        return False

    def _process(self, attempt: int) -> None:
        if self.paused:
            return
        try:
            mime = self._cb.mimeData()
            if mime is None or not mime.formats():
                # буфер занят другим приложением — пробуем ещё раз чуть позже
                if attempt < 2:
                    QTimer.singleShot(250, lambda: self._process(attempt + 1))
                return
            if self._is_protected(mime):
                return
            db = self.ctx.db

            if mime.hasUrls():                       # скопированные файлы
                paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
                if paths:
                    db.add_files(paths)
                    self.changed.emit()
                    return

            text = mime.text() if mime.hasText() else ""
            html = mime.html() if mime.hasHtml() else None
            # «Копировать картинку» в браузере кладёт и картинку, и её адрес/HTML — сохраняем картинку.
            is_image = mime.hasImage() and (
                not text.strip() or (html is not None and "<img" in html.lower() and detect_kind(text) == "link"))
            if is_image:
                image = self._cb.image()
                if not image.isNull():
                    self._pool.submit(self._save_image, db, image)
                    return

            if text.strip():
                if html is not None and len(html) > MAX_HTML:
                    html = None
                db.add_text(text[:MAX_TEXT], html)
                self.changed.emit()
        except Exception:
            log.exception("Ошибка чтения буфера обмена")

    def _save_image(self, db, image: QImage, ts: float | None = None) -> None:
        """Фоновый поток: дедупликация, сохранение PNG, миниатюра, затем OCR."""
        try:
            nbytes = image.sizeInBytes()
            ptr = image.constBits()
            ptr.setsize(nbytes)
            hasher = hashlib.blake2b(digest_size=16)
            hasher.update(f"{image.width()}x{image.height()}x{image.format().value}".encode())
            hasher.update(ptr.asstring(nbytes))
            digest = "i:" + hasher.hexdigest()

            if db.touch_by_hash(digest, bump=ts is None) is not None:      # такая картинка уже есть
                self.changed.emit()
                return

            now = datetime.now()
            rel_dir = Path("Images") / now.strftime("%Y-%m")
            (db.root / rel_dir).mkdir(parents=True, exist_ok=True)
            name = now.strftime("img_%Y%m%d_%H%M%S")
            rel, n = rel_dir / f"{name}.png", 1
            while (db.root / rel).exists():
                rel, n = rel_dir / f"{name}_{n}.png", n + 1
            if not image.save(str(db.root / rel), "PNG", 40):
                raise OSError("не удалось записать PNG")

            make_thumbnail(db, image, rel.as_posix())
            item_id = db.add_image(rel.as_posix(), digest, ts)
            self.changed.emit()

            if self.ctx.settings.get("background_ocr"):
                text = self.ctx.ocr.recognize_qimage(image, fast=True)
                if text:
                    db.set_ocr_text(item_id, text)
                    self.changed.emit()
        except Exception as e:
            if type(e).__name__ == "OcrError":
                if not self._ocr_warned:           # не спамим лог одним и тем же
                    self._ocr_warned = True
                    log.warning("Фоновый OCR недоступен: %s", e)
            else:
                log.exception("Ошибка сохранения изображения")
