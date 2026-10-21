"""Интерфейс (PyQt6): всплывающее окно истории, карточки, QR-код, просмотр
картинок, диалог настроек и значок в трее."""
from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QRect, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QKeySequence,
                         QPainter, QPixmap)
from PyQt6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                             QFormLayout, QFrame, QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSystemTrayIcon, QTabWidget,
                             QToolButton, QVBoxLayout, QWidget)

import calc
import colors
import winutil
from ocr_translator import LangCombo, TranslatePanel
from translator import TARGET_LANGS
from ui_extra import NoteEditor, OcrDiagDialog, TagDialog, save_to_word, text_stats

log = logging.getLogger(__name__)

PAGE_SIZE = 40
KIND_LABELS = {"text": "Текст", "link": "Ссылка", "image": "Изображение", "file": "Файл"}
FILTERS = [("Все", None), ("Текст", "text"), ("Ссылки", "link"), ("Изображения", "image"), ("Избранное", "fav")]


# =====================================================================================
#  Вспомогательное
# =====================================================================================
MERGE_MODES = [("newline", "С новой строки"), ("space", "Через пробел"), ("comma", "Через запятую"),
               ("bullets", "Маркированный список"), ("numbers", "Нумерованный список")]


def join_parts(parts: list[str], mode: str) -> str:
    """Smart Merge: как склеивать несколько выбранных карточек."""
    items = [p.strip() for p in parts if p and p.strip()]
    inline = [" ".join(p.split()) for p in items]              # для строки/списка внутри одной строки переносы не нужны
    if mode == "space":
        return " ".join(inline)
    if mode == "comma":
        return ", ".join(inline)
    if mode == "bullets":
        return "\n".join(f"- {p}" for p in inline)
    if mode == "numbers":
        return "\n".join(f"{i}. {p}" for i, p in enumerate(inline, 1))
    return "\n".join(items)


def human_time(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    today = date.today()
    if d.date() == today:
        return d.strftime("сегодня, %H:%M")
    if d.date() == today - timedelta(days=1):
        return d.strftime("вчера, %H:%M")
    return d.strftime("%d.%m.%Y, %H:%M")


def one_line(text: str, limit: int) -> str:
    s = " ".join(text.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def load_scaled(path, max_w: int, max_h: int) -> QPixmap | None:
    img = QImage(str(path))
    if img.isNull():
        return None
    dpr = QGuiApplication.primaryScreen().devicePixelRatio()
    box = QSize(int(max_w * dpr), int(max_h * dpr))
    if img.width() > box.width() or img.height() > box.height():
        img = img.scaled(box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def paste_into(hwnd) -> None:
    """Возвращает фокус прежнему окну и отправляет Ctrl+V."""
    def go():
        if hwnd and winutil.is_window(hwnd):
            winutil.force_foreground(hwnd)
        QTimer.singleShot(90, winutil.send_ctrl_v)
    QTimer.singleShot(60, go)


def make_qr_pixmap(text: str, target: int = 340) -> QPixmap:
    """QR рисуется напрямую из матрицы (Pillow не нужен)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_L if len(text) > 900 else ERROR_CORRECT_M,
                       border=2, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)                      # DataOverflowError, если текст слишком длинный
    matrix = qr.get_matrix()
    n = len(matrix)
    scale = max(2, target // n)
    img = QImage(n * scale, n * scale, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                p.drawRect(x * scale, y * scale, scale, scale)
    p.end()
    return QPixmap.fromImage(img)


def make_icon(paused: bool = False, secret: bool = False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#8a8f9b" if paused else "#5b8def"))
    p.drawRoundedRect(10, 8, 44, 52, 8, 8)
    p.setBrush(QColor("#1d2026"))
    p.drawRoundedRect(22, 3, 20, 13, 4, 4)
    p.setBrush(QColor("white"))
    for y, w in ((26, 28), (35, 28), (44, 18)):
        p.drawRoundedRect(18, y, w, 4, 2, 2)
    if secret:                                   # небольшая фиолетовая точка — вы в секретном режиме
        p.setBrush(QColor("#9b6bff"))
        p.drawEllipse(38, 38, 24, 24)
    p.end()
    return QIcon(pm)


# =====================================================================================
#  Всплывающая картинка при наведении
# =====================================================================================
class ImagePreview(QLabel):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background:#15181d; border:1px solid #5b8def; border-radius:8px; padding:4px;")
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()

    def pixmap_for(self, path) -> QPixmap | None:
        key = str(path)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        pm = load_scaled(path, 560, 420)
        if pm is not None:
            self._cache[key] = pm
            while len(self._cache) > 6:
                self._cache.popitem(last=False)
        return pm

    def clear_cache(self) -> None:
        self._cache.clear()


# =====================================================================================
#  Окно QR-кода
# =====================================================================================
class QrWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("win")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("QR-код")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(8)
        self.img = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        self.caption.setMaximumWidth(340)
        btn = QPushButton("Закрыть")
        btn.clicked.connect(self.hide)
        lay.addWidget(self.img)
        lay.addWidget(self.caption)
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)

    def show_text(self, text: str) -> None:
        try:
            pm = make_qr_pixmap(text)
            self.img.setPixmap(pm)
            self.caption.setText(one_line(text, 90))
        except Exception:  # слишком длинный текст или нет пакета qrcode
            log.info("QR не создан", exc_info=True)
            self.img.setPixmap(QPixmap())
            self.img.setText("Текст слишком длинный для QR-кода\n(максимум около 2000 символов).")
            self.caption.setText("")
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.center() - self.rect().center())
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide()


# =====================================================================================
#  Просмотр изображения (аналог окна Ножниц после Win+Shift+S)
# =====================================================================================
_open_viewers: set = set()


class ImageViewer(QWidget):
    def __init__(self, ctx, row: dict, prev_hwnd) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.ctx, self.row, self.prev_hwnd = ctx, row, prev_hwnd
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("win")
        self.path = ctx.db.abs_path(row["image_path"])
        self.setWindowTitle(f"Изображение — {self.path.name}")
        self.setMinimumWidth(740)
        self._img = QImage(str(self.path))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.b_copy = QPushButton("Копировать")
        self.b_paste = QPushButton("Вставить")
        self.b_paste.setObjectName("primary")
        self.b_text = QPushButton("Текст")
        self.b_text.setCheckable(True)
        self.b_text.setToolTip("Распознать и показать текст на картинке")
        self.b_tr = QPushButton("Перевести")
        self.b_tr.setToolTip("Распознать текст и перевести на выбранный язык")
        self.lang = LangCombo(ctx)
        self.b_save = QPushButton("Сохранить как…")
        for b in (self.b_copy, self.b_paste, self.b_text, self.b_tr, self.b_save):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar.addWidget(self.b_copy)
        bar.addWidget(self.b_paste)
        bar.addWidget(self.b_text)
        bar.addWidget(self.b_tr)
        bar.addWidget(QLabel("на:"))
        bar.addWidget(self.lang)
        bar.addWidget(self.b_save)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.view = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(1, 1)
        self.view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        lay.addWidget(self.view, 1)

        self.panel = TranslatePanel(ctx, lang_combo=self.lang, compact=True)
        self.panel.hide()
        lay.addWidget(self.panel)

        self.b_copy.clicked.connect(self._copy)
        self.b_paste.clicked.connect(self._paste)
        self.b_text.toggled.connect(self._toggle_panel)
        self.b_tr.clicked.connect(self._translate)
        self.b_save.clicked.connect(self._save)
        self.panel.recognized.connect(self._store_ocr)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._rescale)

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        w = max(740, min(self._img.width() + 40, int(geo.width() * 0.8)))
        h = max(460, min(self._img.height() + 130, int(geo.height() * 0.85)))
        self.resize(w, h)
        self.move(geo.center() - self.rect().center())

    def open(self) -> None:
        _open_viewers.add(self)
        self.destroyed.connect(lambda *_: _open_viewers.discard(self))
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()

    # ---------- текст и перевод ----------
    def _toggle_panel(self, on: bool) -> None:
        self.panel.setVisible(on)
        if on and not self.panel.has_text() and not self.panel.busy:
            self.panel.recognize(self._img)
        self._timer.start()

    def _translate(self) -> None:
        if not self.b_text.isChecked():
            self.b_text.blockSignals(True)
            self.b_text.setChecked(True)
            self.b_text.blockSignals(False)
            self.panel.show()
            self._timer.start()
        if self.panel.has_text():
            self.panel.translate_now()
        else:
            self.panel.recognize(self._img, then_translate=True)

    def _store_ocr(self, text: str) -> None:
        try:
            self.ctx.db.set_ocr_text(self.row["id"], text)     # точный текст улучшает и поиск по истории
        except Exception:
            log.debug("Не удалось сохранить распознанный текст", exc_info=True)

    # ---------- отображение ----------
    def _rescale(self) -> None:
        if self._img.isNull():
            self.view.setText("Не удалось открыть файл изображения")
            return
        dpr = self.devicePixelRatioF()
        box = QSize(int(self.view.width() * dpr), int(self.view.height() * dpr))
        img = self._img
        if img.width() > box.width() or img.height() > box.height():
            img = img.scaled(box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        self.view.setPixmap(pm)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._timer.start()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._timer.start()

    # ---------- буфер обмена ----------
    def _to_clipboard(self) -> None:
        self.ctx.monitor.ignore_next()
        QGuiApplication.clipboard().setImage(self._img)
        self.ctx.db.touch(self.row["id"])

    def _copy(self) -> None:
        self._to_clipboard()
        self.b_copy.setText("Скопировано ✓")
        QTimer.singleShot(1200, lambda: self.b_copy.setText("Копировать"))

    def _paste(self) -> None:
        self._to_clipboard()
        prev = self.prev_hwnd
        self.close()
        paste_into(prev)

    def _save(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Сохранить как", str(Path.home() / self.path.name),
                                              "PNG (*.png);;JPEG (*.jpg)")
        if name and not self._img.save(name):
            QMessageBox.warning(self, "Сохранение", "Не удалось сохранить файл.")

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


# =====================================================================================
#  Предпросмотр длинного текста при наведении на «глазок»
# =====================================================================================
class TextPreview(QLabel):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background:#15181d; border:1px solid #5b8def; border-radius:8px; padding:10px;"
                           "color:#e6e8ec;")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setMinimumWidth(260)
        self.setMaximumWidth(500)
        self.setMaximumHeight(480)

    def show_text(self, text: str) -> None:
        lines = text.strip().splitlines()
        shown = "\n".join(lines[:30])[:1500]
        if len(lines) > 30 or len(text.strip()) > len(shown):
            shown += "\n…"
        self.setText(f"{text_stats(text)}\n\n{shown}")        # счётчик символов/слов/строк для копирайтеров и SMM
        self.adjustSize()


# =====================================================================================
#  Окно полного текста (+ перевод выделенного фрагмента или всего текста)
# =====================================================================================
class TextViewer(QWidget):
    def __init__(self, ctx, row: dict, prev_hwnd) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.ctx, self.row, self.prev_hwnd = ctx, row, prev_hwnd
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("win")
        self.setWindowTitle("Полный текст")
        self.setMinimumWidth(620)
        self.full = ctx.db.full_text(row)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.b_copy = QPushButton("Копировать")
        self.b_paste = QPushButton("Вставить")
        self.b_paste.setObjectName("primary")
        self.b_tr = QPushButton("Перевести")
        self.b_tr.setToolTip("Переводит выделенный фрагмент, а если ничего не выделено — весь текст")
        self.lang = LangCombo(ctx)
        for b in (self.b_copy, self.b_paste, self.b_tr):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            bar.addWidget(b)
        bar.addWidget(QLabel("на:"))
        bar.addWidget(self.lang)
        self.b_word = QPushButton("В Word…")
        self.b_word.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.b_word.setToolTip("Сохранить текст (или выделенный фрагмент) в документ Word")
        bar.addWidget(self.b_word)
        bar.addStretch(1)
        info = QLabel(text_stats(self.full))
        info.setObjectName("muted")
        bar.addWidget(info)
        lay.addLayout(bar)

        self.edit = QPlainTextEdit(self.full)
        self.edit.setReadOnly(True)
        lay.addWidget(self.edit, 1)
        self.panel = TranslatePanel(ctx, lang_combo=self.lang, compact=True, show_source=False)
        self.panel.hide()
        lay.addWidget(self.panel)

        self.b_copy.clicked.connect(self._copy)
        self.b_paste.clicked.connect(self._paste)
        self.b_tr.clicked.connect(self._translate)
        self.b_word.clicked.connect(lambda: save_to_word(self._selection_or_all(), self))

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.resize(min(760, int(geo.width() * 0.7)), min(640, int(geo.height() * 0.8)))
        self.move(geo.center() - self.rect().center())

    def open(self) -> None:
        _open_viewers.add(self)
        self.destroyed.connect(lambda *_: _open_viewers.discard(self))
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()

    def _selection_or_all(self) -> str:
        sel = self.edit.textCursor().selectedText().replace("\u2029", "\n").strip()
        return sel or self.full

    def _to_clipboard(self) -> None:
        self.ctx.monitor.ignore_next()
        QGuiApplication.clipboard().setText(self._selection_or_all())

    def _copy(self) -> None:
        self._to_clipboard()
        self.b_copy.setText("Скопировано ✓")
        QTimer.singleShot(1200, lambda: self.b_copy.setText("Копировать"))

    def _paste(self) -> None:
        self._to_clipboard()
        prev = self.prev_hwnd
        self.close()
        paste_into(prev)

    def _translate(self) -> None:
        self.panel.text.setPlainText(self._selection_or_all())
        self.panel.show()
        self.panel.translate_now()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


# =====================================================================================
#  Загрузка офлайн-модели перевода
# =====================================================================================
class _DownloadThread(QThread):
    progress = pyqtSignal(object, object, str)
    done = pyqtSignal(str)                    # "" — успех, иначе текст ошибки

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        import local_translate as lt
        try:
            lt.download_model(progress=lambda a, b, n: self.progress.emit(a, b, n),
                              cancelled=lambda: self._cancel)
        except lt.DownloadCancelled:
            self.done.emit("Загрузка отменена. Её можно продолжить позже — файл докачается.")
        except Exception as e:  # noqa: BLE001
            log.warning("Загрузка модели не удалась", exc_info=True)
            self.done.emit(f"Ошибка загрузки: {e}")
        else:
            self.done.emit("")


class ModelDownloadDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Офлайн-модель перевода (NLLB-200)")
        self.setMinimumWidth(480)
        self.ok = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(10)
        info = QLabel("Скачивается один раз (≈630 МБ) с huggingface.co. Файлы проверяются по SHA-256. "
                      "Модель — личного, некоммерческого использования (CC-BY-NC 4.0).")
        info.setWordWrap(True)
        info.setObjectName("muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.label = QLabel("Подключаюсь…")
        self.label.setWordWrap(True)
        self.btn = QPushButton("Отмена")
        lay.addWidget(info)
        lay.addWidget(self.bar)
        lay.addWidget(self.label)
        lay.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignRight)
        self.btn.clicked.connect(self._on_button)
        self.thread = _DownloadThread()
        self.thread.progress.connect(self._on_progress)
        self.thread.done.connect(self._on_done)
        self.thread.start()

    def _on_progress(self, done, total, name) -> None:
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            self.label.setText(f"{name}: {done / 1e6:.0f} из {total / 1e6:.0f} МБ")
        else:
            self.bar.setRange(0, 0)
            self.label.setText(f"{name}: {done / 1e6:.0f} МБ")

    def _on_done(self, error: str) -> None:
        self.btn.setText("Закрыть")
        if error:
            self.label.setText(error)
            return
        import local_translate as lt
        self.ok = True
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        ok, deps = lt.deps_status()
        self.label.setText("Готово: модель установлена." if ok else
                           f"Модель скачана. Осталось установить пакеты командой:  {deps}")

    def _on_button(self) -> None:
        if self.thread.isRunning():
            self.thread.cancel()
            self.btn.setEnabled(False)
            self.label.setText("Отменяю…")
        else:
            self.accept()

    def reject(self) -> None:
        if self.thread.isRunning():
            self.thread.cancel()
            self.thread.wait(5000)
        super().reject()


# =====================================================================================
#  Карточка элемента (высота зависит от количества строк текста)
# =====================================================================================
CARD_FONT = QFont("Segoe UI")
CARD_FONT.setPixelSize(13)
LABEL_W = 345        # ширина текста в карточке при окне 400 px
THUMB_W = 122        # миниатюра 112 + отступ 10
WRAP_FLAG = Qt.TextFlag.TextWordWrap.value


def fit_lines(text: str, width: int, max_lines: int) -> tuple[str, int, bool]:
    """Измеряет текст реальным шрифтом: (что показать, сколько строк займёт, обрезан ли)."""
    fm = QFontMetrics(CARD_FONT)

    def count(t: str) -> int:
        return max(1, round(fm.boundingRect(QRect(0, 0, width, 10000), WRAP_FLAG, t).height() / fm.lineSpacing()))

    n = count(text)
    if n <= max_lines:
        return text, n, False
    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if count(text[:mid].rstrip() + "…") <= max_lines:
            lo = mid
        else:
            hi = mid
    return text[:lo].rstrip() + "…", max_lines, True


def short_time(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    today = date.today()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today - timedelta(days=1):
        return d.strftime("вчера %H:%M")
    return d.strftime("%d.%m %H:%M") if d.year == today.year else d.strftime("%d.%m.%Y")


class ElideLabel(QLabel):
    """Подпись, которая сама сокращается «…» под ширину (без неё длинная подпись выдавливала бы кнопки)."""

    def __init__(self) -> None:
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._apply()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply()

    def _apply(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width())))


class HoverButton(QToolButton):
    entered = pyqtSignal()
    left = pyqtSignal()

    def enterEvent(self, e) -> None:
        self.entered.emit()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.left.emit()
        super().leaveEvent(e)


KIND_NAME = {"link": "Ссылка", "note": "Заметка", "file": "Файл", "image": "Изображение"}
SWATCH_W = 32
WORD_BUTTON_MIN = 3000        # с этой длины текста на карточке появляется кнопка «Word»


class ItemCard(QFrame):
    copy_clicked = pyqtSignal(object)
    qr_clicked = pyqtSignal(object)
    pin_clicked = pyqtSignal(object)
    delete_clicked = pyqtSignal(object)
    view_clicked = pyqtSignal(object)
    action = pyqtSignal(str, object, object)    # (действие, строка, доп. данные)
    hovered = pyqtSignal(object, object)        # (row | None, card | None) — предпросмотр картинки
    view_hovered = pyqtSignal(object, object)   # (row | None, кнопка | None) — предпросмотр текста

    def __init__(self, row: dict, thumb: QPixmap | None) -> None:
        super().__init__()
        self.row = row
        kind = row["type"]
        raw = row.get("content") or ""
        self.setObjectName("card")
        self.setProperty("sel", False)
        self.setProperty("tint", row.get("color") or "")
        truncated = False
        is_text = kind in ("text", "link", "note")
        color = colors.parse_color(raw) if kind == "text" and len(raw) <= 40 else None
        calc_result = calc.evaluate(raw) if kind == "text" and len(raw) <= 120 else None

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 6, 4)
        v.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setContentsMargins(0, 0, 0, 0)

        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        line_h = QFontMetrics(CARD_FONT).lineSpacing()

        if kind == "image":
            t = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
            t.setFixedSize(112, 76)
            t.setStyleSheet("background:#1d2026; border-radius:6px;")
            if thumb is not None:
                dpr = self.devicePixelRatioF()
                pm = thumb.scaled(int(112 * dpr), int(76 * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
                pm.setDevicePixelRatio(dpr)
                t.setPixmap(pm)
            top.addWidget(t)
            ocr = " ".join((row.get("ocr_text") or "").split())[:300]
            text, _n, _cut = fit_lines(ocr, LABEL_W - THUMB_W, 4) if ocr else ("Скриншот", 1, False)
            label.setText(text)
            label.setStyleSheet("color:#aeb4c0;" if ocr else "color:#8e95a3;")
            body_h = 76
            label.setFixedHeight(body_h)
        else:
            if kind == "file":
                names = [os.path.basename(p.rstrip("\\/")) or p for p in raw.splitlines() if p]
                shown = names[:3] if len(names) <= 3 else names[:2] + [f"… и ещё {len(names) - 2}"]
                text, n = "\n".join(shown), len(shown)
            else:
                flat = " ".join(raw.split())[:700]
                text, n, truncated = fit_lines(flat, LABEL_W - (SWATCH_W if color else 0), 3)
                if len(raw) > len(flat) or (row.get("length") or 0) > len(flat):
                    truncated = True                    # на карточке только начало — «глазок» покажет всё
                if kind == "link":
                    label.setStyleSheet("color:#7fa8f5;")
            label.setText(text)
            body_h = n * line_h + 2
            label.setFixedHeight(body_h)
            if color:                                   # цветной квадратик рядом с кодом цвета
                r, g, b, a = color
                sw = QFrame()
                sw.setFixedSize(22, 22)
                sw.setToolTip(colors.to_hex(color))
                sw.setStyleSheet(f"background:rgba({r},{g},{b},{a / 255:.2f}); border:1px solid #555b66; "
                                 "border-radius:5px;")
                top.addWidget(sw, 0, Qt.AlignmentFlag.AlignTop)
                body_h = max(body_h, 22)
        col.addWidget(label)
        if calc_result is not None:                     # калькулятор на лету: «= 3000»
            res = QLabel(f"= {calc_result}")
            res.setStyleSheet("color:#8fb4ff; font-weight:600;")
            res.setFixedHeight(line_h + 2)
            res.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            col.addWidget(res)
            body_h += line_h + 2
        top.addLayout(col, 1)
        v.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(0)
        parts = []
        if kind in KIND_NAME:
            parts.append(KIND_NAME[kind])
        if is_text:
            parts.append(f"{(row.get('length') or len(raw)):,} симв.".replace(",", " "))
        parts.append(short_time(row["updated_at"]))
        if row.get("tags"):
            parts.append(row["tags"])
        meta = ElideLabel()
        meta.setObjectName("muted")
        meta.set_full_text(" · ".join(parts))
        bottom.addWidget(meta, 1)

        if truncated:
            eye = HoverButton()
            eye.setObjectName("mini")
            eye.setText("👁")
            eye.setToolTip("Полный текст: наведите — предпросмотр, нажмите — открыть окно")
            eye.setCursor(Qt.CursorShape.PointingHandCursor)
            eye.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            eye.clicked.connect(lambda: self.view_clicked.emit(self.row))
            eye.entered.connect(lambda: self.view_hovered.emit(self.row, eye))
            eye.left.connect(lambda: self.view_hovered.emit(None, None))
            bottom.addWidget(eye)
        if is_text and ((row.get("length") or len(raw)) >= WORD_BUTTON_MIN or row.get("text_path")):
            bottom.addWidget(self._act("Word", "Сохранить в документ Word…", "word"))
        if calc_result is not None:
            bottom.addWidget(self._act("= ⧉", "Скопировать результат", "copy_text", calc_result))
        if color:
            bottom.addWidget(self._color_button(color))
        self.btn_copy = self._button("Копировать", "Скопировать в буфер обмена", self.copy_clicked)
        bottom.addWidget(self.btn_copy)
        if is_text:
            bottom.addWidget(self._button("QR", "Показать QR-код", self.qr_clicked))
        pin = self._button("★" if row["pinned"] else "☆",
                           "Открепить" if row["pinned"] else "Закрепить вверху списка", self.pin_clicked)
        if row["pinned"]:
            pin.setStyleSheet("color:#f5c04a;")
        bottom.addWidget(pin)
        more = self._tool("⋯", "Ещё: редактировать, теги и цвет, Word")
        more.clicked.connect(self._show_menu)
        bottom.addWidget(more)
        bottom.addWidget(self._button("✕", "Удалить из истории", self.delete_clicked))
        v.addLayout(bottom)

        self.fixed_h = 8 + body_h + 2 + 24 + 4          # поля + текст + отступ + ряд кнопок
        self.setFixedHeight(self.fixed_h)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.timeout.connect(lambda: self.btn_copy.setText("Копировать"))

    # ---------- кнопки ----------
    def _tool(self, text: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setObjectName("mini")
        b.setText(text)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return b

    def _button(self, text: str, tip: str, signal) -> QToolButton:
        b = self._tool(text, tip)
        b.clicked.connect(lambda: signal.emit(self.row))
        return b

    def _act(self, text: str, tip: str, name: str, extra=None) -> QToolButton:
        b = self._tool(text, tip)
        b.clicked.connect(lambda: self.action.emit(name, self.row, extra))
        return b

    def _color_button(self, color) -> QToolButton:
        b = self._tool("🎨", "Скопировать цвет в нужном формате")
        b.clicked.connect(lambda: self._color_menu(color))
        return b

    def _color_menu(self, color) -> None:
        menu = QMenu(self)
        for title, fmt in colors.FORMATS:
            value = fmt(color)
            act = menu.addAction(f"{title}   {value}")
            act.triggered.connect(lambda _checked=False, v=value: self.action.emit("copy_text", self.row, v))
        menu.exec(QCursor.pos())

    def _show_menu(self) -> None:
        kind = self.row["type"]
        menu = QMenu(self)
        entries = []
        if kind in ("text", "link", "note"):
            entries.append(("Редактировать…", "edit"))
        entries.append(("Теги и цвет…", "tags"))
        if kind in ("text", "link", "note"):
            entries.append(("Сохранить в Word…", "word"))
        for title, name in entries:
            act = menu.addAction(title)
            act.triggered.connect(lambda _checked=False, n=name: self.action.emit(n, self.row, None))
        menu.exec(QCursor.pos())

    def flash_copied(self) -> None:
        self.btn_copy.setText("Скопировано ✓")
        self._flash.start(1000)

    def set_selected(self, selected: bool) -> None:
        if self.property("sel") != selected:
            self.setProperty("sel", selected)
            self.style().unpolish(self)
            self.style().polish(self)

    def enterEvent(self, e) -> None:
        if self.row["type"] == "image":
            self.hovered.emit(self.row, self)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if self.row["type"] == "image":
            self.hovered.emit(None, None)
        super().leaveEvent(e)


class SearchEdit(QLineEdit):
    down_pressed = pyqtSignal()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Down:
            self.down_pressed.emit()
            return
        super().keyPressEvent(e)


# =====================================================================================
#  Главное окно истории
# =====================================================================================
class HistoryPopup(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.ctx = ctx
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 600)
        self._prev_hwnd = None
        self._shown_at = 0.0
        self._offset = 0
        self._exhausted = False
        self._order: list[int] = []        # порядок выбора для мульти-вставки
        self.preview = ImagePreview()
        self.text_preview = TextPreview()
        self.qr = QrWindow()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.root = QFrame()
        self.root.setObjectName("root")
        self.root.setProperty("secret", False)
        outer.addWidget(self.root)
        lay = QVBoxLayout(self.root)
        lay.setContentsMargins(10, 10, 10, 8)
        lay.setSpacing(8)
        self.badge = QLabel("🔒 Секретный режим — это хранилище отдельное и изолированное")
        self.badge.setObjectName("badge")
        self.badge.hide()
        lay.addWidget(self.badge)

        self.search = SearchEdit()
        self.search.setPlaceholderText("Поиск по истории и тексту на картинках…")
        self.search.setClearButtonEnabled(True)
        srow = QHBoxLayout()
        srow.setSpacing(6)
        self.note_btn = QPushButton("＋ Заметка")
        self.note_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.note_btn.setToolTip("Новая заметка (можно с тегами и цветом); горячая клавиша — в настройках")
        srow.addWidget(self.search, 1)
        srow.addWidget(self.note_btn)
        lay.addLayout(srow)

        frow = QHBoxLayout()
        frow.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, (title, _k) in enumerate(FILTERS):
            b = QPushButton(title)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.group.addButton(b, i)
            frow.addWidget(b)
        self.group.button(0).setChecked(True)
        lay.addLayout(frow)

        drow = QHBoxLayout()
        drow.setSpacing(6)
        self.date = QComboBox()
        self.date.addItems(["За всё время", "Сегодня", "Вчера", "За неделю"])
        self.plain = QPushButton("Plain text")
        self.plain.setCheckable(True)
        self.plain.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.plain.setToolTip("Вставлять текст без форматирования и шрифтов")
        self.plain.setChecked(bool(ctx.settings.get("plain_text")))
        self.tag_combo = QComboBox()
        self.tag_combo.setToolTip("Фильтр по тегам («папки»)")
        self.tag_combo.addItem("Все теги", "")
        drow.addWidget(self.date, 1)
        drow.addWidget(self.tag_combo, 1)
        drow.addWidget(self.plain)
        lay.addLayout(drow)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSpacing(4)
        lay.addWidget(self.list, 1)
        self.empty = QLabel("Здесь пока пусто.\nСкопируйте текст, ссылку или скриншот (Win+Shift+S).",
                            alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("muted")
        lay.addWidget(self.empty, 1)

        brow = QHBoxLayout()
        self.hint = QLabel("Ctrl+клик — выбрать несколько, Enter — вставить")
        self.hint.setObjectName("muted")
        self.paste_btn = QPushButton()
        self.paste_btn.setObjectName("primary")
        self.paste_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.paste_btn.hide()
        self.merge = QComboBox()
        self.merge.setToolTip("Как склеить выбранные карточки при вставке")
        for mode, title in MERGE_MODES:
            self.merge.addItem(title, mode)
        self.merge.setCurrentIndex(max(0, self.merge.findData(ctx.settings.get("merge_mode"))))
        self.merge.hide()
        brow.addWidget(self.hint, 1)
        brow.addWidget(self.merge)
        brow.addWidget(self.paste_btn)
        lay.addLayout(brow)

        # таймеры: дебаунс поиска и освобождение памяти после закрытия
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self.refresh)
        self._free_timer = QTimer(self)
        self._free_timer.setSingleShot(True)
        self._free_timer.setInterval(5000)
        self._free_timer.timeout.connect(self._free_memory)

        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.returnPressed.connect(self._paste_selected)
        self.search.down_pressed.connect(self._focus_list)
        self.group.idClicked.connect(lambda _i: self.refresh())
        self.date.currentIndexChanged.connect(lambda _i: self.refresh())
        self.plain.toggled.connect(lambda on: ctx.settings.set("plain_text", on))
        self.list.itemClicked.connect(self._on_click)
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.paste_btn.clicked.connect(self._paste_selected)
        self.note_btn.clicked.connect(self.new_note)
        self.tag_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        self.merge.currentIndexChanged.connect(lambda _i: ctx.settings.set("merge_mode", self.merge.currentData()))

    # ---------- показ/скрытие ----------
    def toggle(self) -> None:
        if self.isVisible():
            self.hide_popup()
        else:
            self.show_popup()

    def show_popup(self) -> None:
        self._prev_hwnd = winutil.foreground_window()
        getattr(self.ctx, "activity", lambda: None)()
        self._free_timer.stop()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._reload_tags()
        self.refresh()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()           # без панели задач — окно у трея
        self.setFixedHeight(min(600, geo.height() - 24))
        self.move(geo.x() + geo.width() - self.width() - 12, geo.y() + geo.height() - self.height() - 12)
        self._shown_at = time.monotonic()
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()
        self.search.setFocus()

    def hide_popup(self) -> None:
        self.preview.hide()
        self.text_preview.hide()
        self.hide()

    def hideEvent(self, e) -> None:
        self.preview.hide()
        self.text_preview.hide()
        self._free_timer.start()
        super().hideEvent(e)

    def _free_memory(self) -> None:
        if not self.isVisible():
            self.list.clear()
            self._offset = 0
            self.preview.clear_cache()
            winutil.trim_memory()

    def changeEvent(self, e) -> None:
        if (e.type() == QEvent.Type.ActivationChange and self.isVisible() and not self.isActiveWindow()
                and time.monotonic() - self._shown_at > 0.25):
            QTimer.singleShot(0, self._hide_if_inactive)
        super().changeEvent(e)

    def _hide_if_inactive(self) -> None:
        if self.isVisible() and not self.isActiveWindow():
            self.hide_popup()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide_popup()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.focusWidget() is self.list:
            self._paste_selected()
        else:
            super().keyPressEvent(e)

    def _focus_list(self) -> None:
        if self.list.count():
            self.list.setFocus()
            self.list.setCurrentRow(0)

    # ---------- загрузка списка ----------
    def _query_args(self) -> dict:
        fid = self.group.checkedId()
        key = FILTERS[fid][1]
        since = until = None
        today0 = datetime.combine(date.today(), dtime.min).timestamp()
        d = self.date.currentIndex()
        if d == 1:
            since = today0
        elif d == 2:
            since, until = today0 - 86400, today0
        elif d == 3:
            since = time.time() - 7 * 86400
        kind = {"text": ("text", "note")}.get(key, key if key not in (None, "fav") else None)
        return dict(query=self.search.text(), kind=kind, favorites=(key == "fav"), since=since, until=until,
                    tag=self.tag_combo.currentData() or None,
                    pin_limit=int(self.ctx.settings.get("pinned_visible") or 0))

    def refresh(self, keep: bool = False) -> None:
        sb = self.list.verticalScrollBar()
        pos = sb.value() if keep else 0
        count = max(PAGE_SIZE, self._offset) if keep else PAGE_SIZE
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)
        self._offset, self._exhausted = 0, False
        self._order.clear()
        self._load(count)
        if keep:
            sb.setValue(pos)
        self._on_selection()

    def _load(self, count: int) -> None:
        try:
            rows = self.ctx.db.search(limit=count, offset=self._offset, **self._query_args())
        except Exception:
            log.exception("Ошибка чтения истории")
            rows = []
        self._offset += len(rows)
        if len(rows) < count:
            self._exhausted = True
        for r in rows:
            self._add_row(r)
        has = self.list.count() > 0
        self.list.setVisible(has)
        self.empty.setVisible(not has)

    def _on_scroll(self, value: int) -> None:
        sb = self.list.verticalScrollBar()
        if not self._exhausted and value >= sb.maximum() - 160:
            self._load(PAGE_SIZE)

    def _thumb_for(self, row: dict) -> QPixmap | None:
        db = self.ctx.db
        tp = db.thumb_path(row["image_path"])
        if tp.exists():
            return QPixmap(str(tp))
        src = QImage(str(db.abs_path(row["image_path"])))     # старые/потерянные миниатюры создаём на лету
        if src.isNull():
            return None
        from clipboard_monitor import make_thumbnail
        make_thumbnail(db, src, row["image_path"])
        return QPixmap(str(tp)) if tp.exists() else None

    def _add_row(self, row: dict) -> None:
        thumb = self._thumb_for(row) if row["type"] == "image" and row.get("image_path") else None
        card = ItemCard(row, thumb)
        card.copy_clicked.connect(self._on_copy)
        card.qr_clicked.connect(self._on_qr)
        card.pin_clicked.connect(self._on_pin)
        card.delete_clicked.connect(self._on_delete)
        card.hovered.connect(self._on_hover)
        card.view_clicked.connect(self._open_text)
        card.action.connect(self._on_action)
        card.view_hovered.connect(self._on_text_hover)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, row)
        item.setSizeHint(QSize(card.sizeHint().width(), card.fixed_h))   # высота строго по карточке
        self.list.addItem(item)
        self.list.setItemWidget(item, card)

    def on_data_changed(self) -> None:
        if self.isVisible():
            QTimer.singleShot(0, lambda: self.refresh(keep=True))

    # ---------- выбор и вставка ----------
    def _on_selection(self) -> None:
        current = [it.data(Qt.ItemDataRole.UserRole)["id"] for it in self.list.selectedItems()]
        self._order = [i for i in self._order if i in current] + [i for i in current if i not in self._order]
        for i in range(self.list.count()):
            item = self.list.item(i)
            card = self.list.itemWidget(item)
            if card is not None:
                card.set_selected(item.isSelected())
        n = len(current)
        self.paste_btn.setVisible(n > 0)
        self.merge.setVisible(n > 1)
        if n:
            self.paste_btn.setText(f"Вставить ({n})")
            self.hint.setText("Склеить в порядке выбора")
        else:
            self.hint.setText("Ctrl+клик — выбрать несколько, Enter — вставить")

    def _on_click(self, item: QListWidgetItem) -> None:
        mods = QGuiApplication.keyboardModifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return                                   # накапливаем выбор для мульти-вставки
        self._paste_rows([item.data(Qt.ItemDataRole.UserRole)])

    def _selected_rows(self) -> list[dict]:
        rows = {}
        for it in self.list.selectedItems():
            r = it.data(Qt.ItemDataRole.UserRole)
            rows[r["id"]] = r
        return [rows[i] for i in self._order if i in rows]

    def _paste_selected(self) -> None:
        rows = self._selected_rows()
        if not rows and self.list.count():
            rows = [self.list.item(0).data(Qt.ItemDataRole.UserRole)]
        if rows:
            self._paste_rows(rows)

    def _build_mime(self, rows: list[dict]) -> QMimeData | None:
        db = self.ctx.db
        mime = QMimeData()
        if len(rows) == 1:
            r = rows[0]
            if r["type"] == "image":
                img = QImage(str(db.abs_path(r["image_path"])))
                if img.isNull():
                    return None
                mime.setImageData(img)
            elif r["type"] == "file":
                mime.setUrls([QUrl.fromLocalFile(p) for p in r["content"].splitlines() if p])
            else:
                mime.setText(db.full_text(r))
                if r.get("html") and not self.plain.isChecked():
                    mime.setHtml(r["html"])
            return mime
        parts = []
        for r in rows:                               # мульти-вставка: склеенный чистый текст
            if r["type"] in ("text", "link", "note"):
                parts.append(db.full_text(r))
            elif r["type"] == "file":
                parts.append(r["content"])
        if not parts:
            return None
        mime.setText(join_parts(parts, self.merge.currentData() or "newline"))
        return mime

    def _put(self, mime: QMimeData, rows: list[dict]) -> None:
        self.ctx.monitor.ignore_next()
        QGuiApplication.clipboard().setMimeData(mime)
        for r in rows:
            self.ctx.db.touch(r["id"])

    def _paste_rows(self, rows: list[dict]) -> None:
        if len(rows) == 1 and rows[0]["type"] == "image":
            self._open_image(rows[0])                # картинка открывается в окне просмотра
            return
        mime = self._build_mime(rows)
        if mime is None:
            self.hint.setText("Нечего вставлять в текстовом виде")
            return
        self._put(mime, rows)
        prev = self._prev_hwnd
        self.hide_popup()
        paste_into(prev)

    def _open_image(self, row: dict) -> None:
        prev = self._prev_hwnd
        self.hide_popup()
        viewer = ImageViewer(self.ctx, row, prev)
        viewer.open()

    # ---------- кнопки карточки ----------
    def _on_copy(self, row: dict) -> None:
        mime = self._build_mime([row])
        if mime is None:
            return
        self._put(mime, [row])
        card = self.sender()
        if isinstance(card, ItemCard):
            card.flash_copied()

    def _on_qr(self, row: dict) -> None:
        text = self.ctx.db.full_text(row)
        self.qr.show_text(text)

    def _on_pin(self, row: dict) -> None:
        self.ctx.db.toggle_pin(row["id"])
        QTimer.singleShot(0, self.refresh)

    def _on_delete(self, row: dict) -> None:
        self.ctx.db.delete(row["id"])
        QTimer.singleShot(0, lambda: self.refresh(keep=True))

    def _reload_tags(self) -> None:
        current = self.tag_combo.currentData() or ""
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("Все теги", "")
        for tag, count in self.ctx.db.all_tags():
            self.tag_combo.addItem(f"{tag} ({count})", tag)
        self.tag_combo.setCurrentIndex(max(0, self.tag_combo.findData(current)))
        self.tag_combo.blockSignals(False)

    def new_note(self) -> None:
        self.hide_popup()
        QTimer.singleShot(0, lambda: NoteEditor(self.ctx).open())

    def _later(self, fn) -> None:
        """Диалоги открываем после скрытия окна истории (иначе потеря фокуса закроет их вместе с ним)."""
        self.hide_popup()
        QTimer.singleShot(0, fn)

    def _on_action(self, name: str, row: dict, extra) -> None:
        if name == "copy_text":                       # результат калькулятора / цвет в выбранном формате
            mime = QMimeData()
            mime.setText(str(extra))
            self._put(mime, [row])
            card = self.sender()
            if isinstance(card, ItemCard):
                card.flash_copied()
        elif name == "edit":
            self._later(lambda: NoteEditor(self.ctx, row).open())
        elif name == "tags":
            self._later(lambda: self._edit_tags(row))
        elif name == "word":
            self._later(lambda: save_to_word(self.ctx.db.full_text(row)))

    def _edit_tags(self, row: dict) -> None:
        dlg = TagDialog(self.ctx, row)
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if dlg.exec():
            tags, color = dlg.values()
            self.ctx.db.set_meta(row["id"], tags=tags, color=color)
            self.ctx.monitor.changed.emit()

    def set_secret(self, on: bool) -> None:
        """Секретный режим: другая рамка и плашка, чтобы всегда было видно, где вы находитесь."""
        self.root.setProperty("secret", on)
        self.root.style().unpolish(self.root)
        self.root.style().polish(self.root)
        self.badge.setVisible(on)
        self.list.clear()
        self._offset = 0
        self.preview.clear_cache()                  # картинки другого профиля не должны оставаться в памяти
        self.text_preview.hide()

    def _open_text(self, row: dict) -> None:
        prev = self._prev_hwnd
        self.hide_popup()
        TextViewer(self.ctx, row, prev).open()

    def _on_text_hover(self, row, button) -> None:
        if row is None:
            self.text_preview.hide()
            return
        self.text_preview.show_text(self.ctx.db.full_text(row))
        gp = button.mapToGlobal(QPoint(0, 0))
        geo = (QGuiApplication.screenAt(gp) or QGuiApplication.primaryScreen()).availableGeometry()
        frame = self.frameGeometry()
        x = frame.left() - self.text_preview.width() - 10
        if x < geo.left():
            x = frame.right() + 10
        y = min(max(gp.y() - self.text_preview.height() // 2, geo.top() + 8),
                geo.bottom() - self.text_preview.height() - 8)
        self.text_preview.move(x, y)
        self.text_preview.show()

    def _on_hover(self, row, card) -> None:
        if row is None:
            self.preview.hide()
            return
        pm = self.preview.pixmap_for(self.ctx.db.abs_path(row["image_path"]))
        if pm is None:
            return
        self.preview.setPixmap(pm)
        self.preview.adjustSize()
        gp = card.mapToGlobal(QPoint(0, 0))
        geo = (QGuiApplication.screenAt(gp) or QGuiApplication.primaryScreen()).availableGeometry()
        frame = self.frameGeometry()
        x = frame.left() - self.preview.width() - 10
        if x < geo.left():
            x = frame.right() + 10
        y = min(max(gp.y() - 20, geo.top() + 8), geo.bottom() - self.preview.height() - 8)
        self.preview.move(x, y)
        self.preview.show()


# =====================================================================================
#  Диалог настроек
# =====================================================================================
class SettingsDialog(QDialog):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        s = ctx.settings
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(640)
        try:
            self._ocr_languages = ctx.ocr.languages()
        except Exception:
            log.info("Список языков OCR недоступен", exc_info=True)
            self._ocr_languages = []

        tabs = QTabWidget()
        tabs.addTab(self._tab_main(s), "Основное")
        tabs.addTab(self._tab_translate(s), "Распознавание и перевод")
        tabs.addTab(self._tab_overlay(s), "Перевод поверх экрана")
        tabs.addTab(self._tab_secret(s), "Секретный режим")

        ok, cancel = QPushButton("Сохранить"), QPushButton("Отмена")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(12)
        lay.addWidget(tabs)
        lay.addLayout(btns)

    # ---------- помощники ----------
    @staticmethod
    def _page(form: QFormLayout) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        form.setSpacing(10)
        outer.addLayout(form)
        outer.addStretch(1)
        return page

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setWordWrap(True)
        return lbl

    @staticmethod
    def _hotkey(value: str) -> QKeySequenceEdit:
        edit = QKeySequenceEdit(QKeySequence(value))
        if hasattr(edit, "setMaximumSequenceLength"):
            edit.setMaximumSequenceLength(1)
        return edit

    def _ocr_lang_combo(self, auto_text: str, current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem(auto_text, "auto")
        for tag, name in self._ocr_languages:
            combo.addItem(f"Только {name} ({tag})", tag)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        return combo

    @staticmethod
    def _seq(edit: QKeySequenceEdit) -> str:
        return edit.keySequence().toString().split(",")[0].strip()

    # ---------- вкладки ----------
    def _tab_main(self, s) -> QWidget:
        self.root = QLineEdit(s.get("root"))
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root, 1)
        root_row.addWidget(browse)
        self.hk_history = self._hotkey(s.get("hotkey_history"))
        self.hk_ocr = self._hotkey(s.get("hotkey_ocr"))
        self.hk_note = self._hotkey(s.get("note_hotkey"))
        self.pin_limit = QSpinBox()
        self.pin_limit.setRange(0, 10)
        self.pin_limit.setSpecialValueText("не поднимать наверх")
        self.pin_limit.setValue(int(s.get("pinned_visible") or 0))
        self.import_hist = QCheckBox("При запуске подтянуть историю буфера Windows (Win+V), если она включена")
        self.import_hist.setChecked(bool(s.get("import_history")))
        self.cleanup = QComboBox()
        for title, days in (("Не удалять", 0), ("Старше 30 дней", 30), ("Старше 60 дней", 60),
                            ("Старше 90 дней", 90)):
            self.cleanup.addItem(title, days)
        self.cleanup.setCurrentIndex(max(0, self.cleanup.findData(s.get("cleanup_days"))))
        self.bg_ocr = QCheckBox("Распознавать текст на скриншотах в фоне (поиск по картинкам)")
        self.bg_ocr.setChecked(bool(s.get("background_ocr")))
        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.autostart.setChecked(bool(s.get("autostart")))
        clear = QPushButton("Очистить историю (кроме закреплённых, заметок и помеченных)")
        clear.clicked.connect(self._clear)

        form = QFormLayout()
        form.addRow("Папка хранения:", root_row)
        form.addRow("Окно истории:", self.hk_history)
        form.addRow("Сканер экрана (OCR):", self.hk_ocr)
        form.addRow("Новая заметка:", self.hk_note)
        form.addRow("Закреплённых наверху:", self.pin_limit)
        form.addRow("", self._hint("Столько закреплённых показывается над списком во вкладках «Все», «Текст», «Ссылки», "
                                   "«Изображения». Остальные закреплённые остаются на своих местах, а во вкладке "
                                   "«Избранное» видны все."))
        form.addRow("Автоочистка:", self.cleanup)
        form.addRow("", self._hint("Автоочистка не удаляет закреплённое, заметки и карточки с тегами или цветом."))
        form.addRow("", self.bg_ocr)
        form.addRow("", self.import_hist)
        form.addRow("", self.autostart)
        form.addRow("", clear)
        return self._page(form)

    def _tab_translate(self, s) -> QWidget:
        import local_translate as lt
        self.tr_to = QComboBox()
        for title, code in TARGET_LANGS:
            self.tr_to.addItem(title, code)
        self.tr_to.setCurrentIndex(max(0, self.tr_to.findData(s.get("translate_to"))))
        self.cur_combo = QComboBox()
        for code in ("RUB", "USD", "EUR", "GBP", "CNY", "KZT", "UAH", "TRY"):
            self.cur_combo.addItem(code, code)
        self.cur_combo.setCurrentIndex(max(0, self.cur_combo.findData(s.get("convert_currency"))))
        self.tr_mode = QComboBox()
        self.tr_mode.addItem("Авто: сначала быстрый онлайн, потом офлайн-модель", "auto")
        self.tr_mode.addItem("Сначала офлайн-модель (быстрее, без блокировок, приватно)", "offline_first")
        self.tr_mode.setCurrentIndex(max(0, self.tr_mode.findData(s.get("translate_mode"))))
        self.auto_tr = QCheckBox("Переводить сразу после распознавания (окно Alt+S)")
        self.auto_tr.setChecked(bool(s.get("auto_translate")))

        self.model_status = QLabel("Офлайн-модель: " + lt.status_text())
        self.model_status.setWordWrap(True)
        dl = QPushButton("Скачать офлайн-модель (≈630 МБ)…")
        dl.clicked.connect(self._download_model)

        self.ocr_lang = self._ocr_lang_combo("Авто — все установленные языки Windows OCR", s.get("ocr_lang"))
        self.tess_path = QLineEdit(s.get("tesseract_path") or "")
        self.tess_path.setPlaceholderText("пусто = искать автоматически")
        tess_browse = QPushButton("Обзор…")
        tess_browse.clicked.connect(self._browse_tess)
        tess_row = QHBoxLayout()
        tess_row.addWidget(self.tess_path, 1)
        tess_row.addWidget(tess_browse)
        try:
            tess_status = "Tesseract: " + self.ctx.ocr.tesseract_status()
        except Exception:
            tess_status = "Tesseract: не определён"
        self.ocrspace = QLineEdit(s.get("ocrspace_key") or "")
        self.ocrspace.setPlaceholderText("необязательно, бесплатный ключ на ocr.space/ocrapi/freekey")

        form = QFormLayout()
        form.addRow("Переводить на:", self.tr_to)
        form.addRow("Суммы конвертировать в:", self.cur_combo)
        form.addRow("Режим перевода:", self.tr_mode)
        form.addRow("", self.auto_tr)
        form.addRow("Офлайн-перевод:", self.model_status)
        form.addRow("", dl)
        form.addRow("", self._hint("Нужны пакеты:  pip install ctranslate2 sentencepiece. Модель NLLB-200 переводит "
                                   "прямо с 200 языков (корейский, японский, китайский...) на любой выбранный."))
        form.addRow("Windows OCR:", self.ocr_lang)
        form.addRow("Путь к Tesseract:", tess_row)
        form.addRow("", self._hint(tess_status))
        diag = QPushButton("Проверить движки OCR…")
        diag.setToolTip("Рисует тестовую картинку и показывает, что распознал каждый движок и насколько точно")
        diag.clicked.connect(lambda: OcrDiagDialog(self.ctx, self).exec())
        form.addRow("", diag)
        form.addRow("Ключ OCR.space:", self.ocrspace)
        form.addRow("", self._hint("Онлайн-OCR работает только при ручном распознавании: изображение отправляется "
                                   "на сервер OCR.space. В фоне интернет не используется."))
        return self._page(form)

    def _tab_overlay(self, s) -> QWidget:
        self.hk_overlay = self._hotkey(s.get("overlay_hotkey"))
        self.ov_interval = QSpinBox()
        self.ov_interval.setRange(500, 5000)
        self.ov_interval.setSingleStep(100)
        self.ov_interval.setSuffix(" мс")
        self.ov_interval.setValue(int(s.get("overlay_interval_ms") or 1200))
        self.ov_lang = self._ocr_lang_combo("Авто — до 3 установленных языков", s.get("overlay_ocr_lang"))
        form = QFormLayout()
        form.addRow("Горячая клавиша:", self.hk_overlay)
        form.addRow("Проверять экран каждые:", self.ov_interval)
        form.addRow("Язык текста на экране:", self.ov_lang)
        form.addRow("", self._hint(
            "Нажмите горячую клавишу и выделите область (двойной щелчок не нужен: для всего экрана есть пункт в "
            "меню трея). Перевод накладывается поверх исходного текста; повторное нажатие выключает режим.\n\n"
            "Для быстрой работы выберите конкретный язык (например, корейский) и скачайте офлайн-модель. "
            "Распознавание запускается только когда картинка изменилась. Игры в «эксклюзивном полноэкранном» "
            "режиме перекрывают наложение — включите режим «окно без рамки»."))
        return self._page(form)

    def _tab_secret(self, s) -> QWidget:
        self.secret_on = QCheckBox("Включить секретный режим")
        self.secret_on.setChecked(bool(s.get("secret_enabled")))
        self.hk_secret = self._hotkey(s.get("secret_hotkey"))
        self.secret_timeout = QSpinBox()
        self.secret_timeout.setRange(0, 240)
        self.secret_timeout.setSuffix(" мин")
        self.secret_timeout.setSpecialValueText("не выходить")
        self.secret_timeout.setValue(int(s.get("secret_timeout_min") or 0))
        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        has_pin = bool(s.get("secret_pin"))
        self.pin.setPlaceholderText("PIN задан — введите новый, чтобы заменить" if has_pin
                                    else "необязательно: PIN для входа")
        self.pin_clear = QCheckBox("Убрать PIN")
        self.pin_clear.setVisible(has_pin)
        form = QFormLayout()
        form.addRow("", self.secret_on)
        form.addRow("Горячая клавиша:", self.hk_secret)
        form.addRow("Автовыход через:", self.secret_timeout)
        form.addRow("PIN:", self.pin)
        form.addRow("", self.pin_clear)
        form.addRow("", self._hint(
            "Секретный режим — отдельное изолированное хранилище (своя база, свои картинки и тексты в скрытой "
            "папке). Пока он включён, всё скопированное сохраняется только туда и в окне истории видно только "
            "оно; основная история не показывается и не затрагивается. Рамка окна становится фиолетовой, "
            "в трее появляется точка. Выход — той же клавишей или из меню трея. PIN хранится только в виде "
            "хеша. Пункт в меню трея виден лишь когда режим включён."))
        return self._page(form)

    # ---------- действия ----------
    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для истории", self.root.text())
        if path:
            self.root.setText(path)

    def _browse_tess(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "tesseract.exe", self.tess_path.text(),
                                              "tesseract.exe (tesseract.exe)")
        if path:
            self.tess_path.setText(path)

    def _download_model(self) -> None:
        import local_translate as lt
        ModelDownloadDialog(self).exec()
        self.model_status.setText("Офлайн-модель: " + lt.status_text())

    def _clear(self) -> None:
        if QMessageBox.question(self, "Очистка", "Удалить всю историю, кроме закреплённых элементов?") \
                == QMessageBox.StandardButton.Yes:
            n = self.ctx.db.clear_all(keep_pinned=True)
            QMessageBox.information(self, "Очистка", f"Удалено элементов: {n}")

    def accept(self) -> None:
        keys = [("Окно истории", self._seq(self.hk_history)), ("Сканер экрана", self._seq(self.hk_ocr)),
                ("Перевод поверх экрана", self._seq(self.hk_overlay)), ("Новая заметка", self._seq(self.hk_note))]
        if self.secret_on.isChecked():
            keys.append(("Секретный режим", self._seq(self.hk_secret)))
        seen = set()
        for title, seq in keys:
            try:
                winutil.parse_hotkey(seq)
            except ValueError as e:
                QMessageBox.warning(self, "Горячие клавиши", f"«{title}»: некорректное сочетание — {e}.")
                return
            if seq.lower() in seen:
                QMessageBox.warning(self, "Горячие клавиши", "Сочетания должны быть разными.")
                return
            seen.add(seq.lower())
        if not self.root.text().strip():
            QMessageBox.warning(self, "Папка хранения", "Укажите папку для хранения истории.")
            return
        super().accept()

    def values(self) -> dict:
        return {
            "root": os.path.normpath(self.root.text().strip()),
            "hotkey_history": self._seq(self.hk_history),
            "hotkey_ocr": self._seq(self.hk_ocr),
            "autostart": self.autostart.isChecked(),
            "background_ocr": self.bg_ocr.isChecked(),
            "cleanup_days": self.cleanup.currentData(),
            "ocr_lang": self.ocr_lang.currentData(),
            "translate_to": self.tr_to.currentData(),
            "translate_mode": self.tr_mode.currentData(),
            "auto_translate": self.auto_tr.isChecked(),
            "tesseract_path": self.tess_path.text().strip(),
            "ocrspace_key": self.ocrspace.text().strip(),
            "note_hotkey": self._seq(self.hk_note),
            "pinned_visible": self.pin_limit.value(),
            "import_history": self.import_hist.isChecked(),
            "convert_currency": self.cur_combo.currentData(),
            "overlay_hotkey": self._seq(self.hk_overlay),
            "overlay_interval_ms": self.ov_interval.value(),
            "overlay_ocr_lang": self.ov_lang.currentData(),
            "secret_enabled": self.secret_on.isChecked(),
            "secret_hotkey": self._seq(self.hk_secret),
            "secret_timeout_min": self.secret_timeout.value(),
            "secret_pin_new": self.pin.text() or None,
            "secret_pin_clear": self.pin_clear.isChecked(),
        }


# =====================================================================================
#  Трей
# =====================================================================================
class TrayIcon(QSystemTrayIcon):
    open_history = pyqtSignal()
    scan_screen = pyqtSignal()
    new_note = pyqtSignal()
    overlay_region = pyqtSignal()
    overlay_full = pyqtSignal()
    open_settings = pyqtSignal()
    quit_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    secret_toggled = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__(make_icon())
        self._paused = False
        self._secret = False
        self.setToolTip("Умный буфер обмена")
        self._menu = QMenu()
        self.a_open = self._menu.addAction("Открыть историю")
        self.a_scan = self._menu.addAction("Сканировать экран")
        self.a_note = self._menu.addAction("Новая заметка…")
        self.a_overlay = self._menu.addAction("Перевод поверх экрана (область)")
        self.a_overlay_full = self._menu.addAction("Перевод поверх экрана (весь экран)")
        self._menu.addSeparator()
        self.a_secret = self._menu.addAction("Секретный режим")
        self.a_secret.setCheckable(True)
        self.a_secret.setVisible(False)                     # пункт виден, только если режим включён в настройках
        self.a_settings = self._menu.addAction("Настройки…")
        self.a_pause = self._menu.addAction("Пауза (инкогнито)")
        self.a_pause.setCheckable(True)
        self._menu.addSeparator()
        self.a_quit = self._menu.addAction("Выход")
        self.setContextMenu(self._menu)
        self._hk = ("Alt+V", "Alt+S", "Alt+T")
        self._overlay_on = False

        self.a_open.triggered.connect(self.open_history)
        self.a_scan.triggered.connect(self.scan_screen)
        self.a_note.triggered.connect(self.new_note)
        self.a_overlay.triggered.connect(self.overlay_region)
        self.a_overlay_full.triggered.connect(self.overlay_full)
        self.a_settings.triggered.connect(self.open_settings)
        self.a_pause.toggled.connect(self._on_pause)
        self.a_secret.toggled.connect(self.secret_toggled)
        self.a_quit.triggered.connect(self.quit_requested)
        self.activated.connect(self._on_activated)

    def set_hotkeys(self, history: str, ocr: str, overlay: str) -> None:
        self._hk = (history, ocr, overlay)
        self._retitle()

    def _retitle(self) -> None:
        history, ocr, overlay = self._hk
        self.a_open.setText(f"Открыть историю ({history})")
        self.a_scan.setText(f"Сканировать экран ({ocr})")
        if self._overlay_on:
            self.a_overlay.setText(f"Остановить перевод поверх экрана ({overlay})")
            self.a_overlay_full.setText("Остановить перевод поверх экрана")
        else:
            self.a_overlay.setText(f"Перевод поверх экрана — область ({overlay})")
            self.a_overlay_full.setText("Перевод поверх экрана — весь экран")

    def set_overlay_active(self, active: bool) -> None:
        self._overlay_on = active
        self._retitle()

    def set_secret_available(self, available: bool) -> None:
        self.a_secret.setVisible(available)

    def set_secret(self, on: bool) -> None:
        """Синхронизирует галочку и значок без повторного срабатывания сигнала."""
        self._secret = on
        self.a_secret.blockSignals(True)
        self.a_secret.setChecked(on)
        self.a_secret.blockSignals(False)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        self.setIcon(make_icon(self._paused, self._secret))
        self.setToolTip("Умный буфер обмена — пауза" if self._paused else "Умный буфер обмена")

    def _on_pause(self, paused: bool) -> None:
        self._paused = paused
        self._refresh_icon()
        self.pause_toggled.emit(paused)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_history.emit()
