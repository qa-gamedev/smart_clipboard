"""Интерфейс распознавания и перевода: захват области экрана (Alt+S), общая панель
«текст + перевод + выбор языка», окно результата.

Сами движки лежат в ocr_engines.py, переводчики — в translator.py.
"""
from __future__ import annotations

import logging
import re
import threading

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPainter, QPen
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
                             QWidget)

import converters
import winutil
from ocr_engines import OcrError
from translator import LANG_RU, TARGET_LANGS, detect_language, reset_cooldowns, translate

log = logging.getLogger(__name__)


# =====================================================================================
#  Запуск функций в фоне с доставкой результата в GUI-поток
# =====================================================================================
class _Done(QObject):
    done = pyqtSignal(object, object)   # (результат, исключение)


_pending: set = set()


def run_async(fn, callback, *args) -> None:
    """fn(*args) выполняется в потоке, callback(result, error) — в GUI-потоке.
    Вызывать только из GUI-потока."""
    emitter = _Done()
    _pending.add(emitter)

    def finish(result, error):
        _pending.discard(emitter)
        callback(result, error)

    emitter.done.connect(finish)

    def job():
        try:
            res, err = fn(*args), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        emitter.done.emit(res, err)

    threading.Thread(target=job, daemon=True, name="async-job").start()


# =====================================================================================
#  Захват области экрана (Alt+S)
# =====================================================================================
class SelectionOverlay(QWidget):
    """Затемнённый экран с крестиком; пользователь тянет прямоугольник."""
    finished = pyqtSignal(object, object)   # (QImage | None, QRect | None) — глобальные координаты

    def __init__(self, screen, pixmap) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._pm = pixmap
        self._pm.setDevicePixelRatio(1.0)      # работаем в физических пикселях
        self._k = pixmap.width() / max(1, screen.geometry().width())
        self._p0: QPoint | None = None
        self._p1: QPoint | None = None

    def _sel(self) -> QRect | None:
        return QRect(self._p0, self._p1).normalized() if self._p0 and self._p1 else None

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._pm)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        r = self._sel()
        if r and r.width() > 1 and r.height() > 1:
            k = self._k
            src = QRect(int(r.x() * k), int(r.y() * k), int(r.width() * k), int(r.height() * k))
            p.drawPixmap(r, self._pm, src)     # выделенная область — без затемнения
            p.setPen(QPen(QColor("#5b8def"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setPen(QColor("white"))
            p.fillRect(QRect(r.x(), max(0, r.y() - 22), 86, 20), QColor(0, 0, 0, 170))
            p.drawText(r.x() + 6, max(14, r.y() - 7), f"{src.width()}×{src.height()}")

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._p0 = self._p1 = e.position().toPoint()
        else:
            self.finished.emit(None, None)

    def mouseMoveEvent(self, e) -> None:
        if self._p0:
            self._p1 = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        r = self._sel()
        if e.button() != Qt.MouseButton.LeftButton or r is None:
            return
        if r.width() < 6 or r.height() < 6:    # случайный клик
            self._p0 = self._p1 = None
            self.update()
            return
        k = self._k
        src = QRect(int(r.x() * k), int(r.y() * k), int(r.width() * k), int(r.height() * k))
        image = self._pm.copy(src).toImage()
        self.finished.emit(image, QRect(self.mapToGlobal(r.topLeft()), r.size()))

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.finished.emit(None, None)


class ScreenGrabber(QObject):
    captured = pyqtSignal(object, object)   # (QImage, QRect)

    def __init__(self) -> None:
        super().__init__()
        self._overlays: list[SelectionOverlay] = []

    def start(self) -> None:
        if self._overlays:
            return
        for screen in QGuiApplication.screens():
            ov = SelectionOverlay(screen, screen.grabWindow(0))
            ov.finished.connect(self._finish)
            ov.show()
            self._overlays.append(ov)
        target = next((o for o in self._overlays if o.geometry().contains(self._cursor())), self._overlays[0])
        target.raise_()
        winutil.force_foreground(int(target.winId()))
        target.activateWindow()
        target.setFocus()

    @staticmethod
    def _cursor() -> QPoint:
        from PyQt6.QtGui import QCursor
        return QCursor.pos()

    def _finish(self, image, rect) -> None:
        overlays, self._overlays = self._overlays, []
        for ov in overlays:
            ov.hide()
            ov.deleteLater()
        if image is not None:
            QTimer.singleShot(50, lambda: self.captured.emit(image, rect))


# =====================================================================================
#  Выбор языка перевода и общая панель «текст + перевод»
# =====================================================================================
class LangCombo(QComboBox):
    """На какой язык переводить. Начальное значение — из настроек (по умолчанию русский)."""

    def __init__(self, ctx) -> None:
        super().__init__()
        for title, code in TARGET_LANGS:
            self.addItem(title, code)
        self.setCurrentIndex(max(0, self.findData(ctx.settings.get("translate_to"))))
        self.setToolTip("На какой язык переводить")
        self.setMinimumWidth(110)

    def code(self) -> str:
        return self.currentData() or "ru"


class TranslatePanel(QWidget):
    """Распознанный текст (можно править) + кнопки копирования/перевода + результат перевода.
    Используется и в окне Alt+S, и под картинкой в окне просмотра."""
    recognized = pyqtSignal(str)

    def __init__(self, ctx, lang_combo: LangCombo | None = None, compact: bool = False,
                 show_source: bool = True) -> None:
        super().__init__()
        self.ctx = ctx
        self.busy = False
        self._failed = False
        self._then_translate = False
        self._last_langs: list[str] = []
        self._url = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Здесь появится распознанный текст")
        self.tr_text = QPlainTextEdit()
        self.tr_text.setReadOnly(True)
        self.tr_text.setPlaceholderText("Здесь появится перевод")
        if compact:
            self.text.setMaximumHeight(110)
            self.tr_text.setMaximumHeight(110)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_copy = QPushButton("Скопировать текст")
        self.btn_copy.setObjectName("primary")
        self.btn_tr = QPushButton("Перевести")
        self.btn_copy_tr = QPushButton("Скопировать перевод")
        self.btn_swap = QPushButton("⇄")
        self.btn_swap.setFixedWidth(40)
        self.btn_swap.setToolTip("Поменять языки местами: перевод станет исходным текстом, а переводить будем на язык "
                                 "оригинала.\nУдобно, чтобы набрать ответ по-русски и скопировать его на языке собеседника.")
        self.btn_open = QPushButton("Открыть ссылку")
        self.btn_open.hide()
        for b in (self.btn_copy, self.btn_tr, self.btn_copy_tr, self.btn_swap, self.btn_open):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lang = lang_combo if lang_combo is not None else LangCombo(ctx)
        row.addWidget(self.btn_copy)
        if lang_combo is None:                       # в окне просмотра комбобокс стоит на панели инструментов
            row.addWidget(QLabel("Перевести на:"))
            row.addWidget(self.lang)
        row.addWidget(self.btn_swap)
        row.addWidget(self.btn_tr)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        self.conv = QLabel()                         # конвертер валют и величин
        self.conv.setObjectName("conv")
        self.conv.setWordWrap(True)
        self.conv.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.conv.hide()

        lay.addWidget(self.status)
        lay.addWidget(self.text, 3)
        lay.addLayout(row)
        lay.addWidget(self.tr_text, 3)
        lay.addWidget(self.btn_copy_tr, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.conv)
        self._show_translation(False)
        self._set_enabled(False)
        if not show_source:                  # окно полного текста: исходник уже виден выше
            self.text.hide()
            self.btn_copy.hide()
            self.btn_swap.hide()

        self.btn_copy.clicked.connect(self._copy_text)
        self.btn_tr.clicked.connect(self.translate_now)
        self.btn_copy_tr.clicked.connect(self._copy_translation)
        self.btn_swap.clicked.connect(self.reverse)
        self.btn_open.clicked.connect(self._open_url)
        self.text.textChanged.connect(lambda: self._set_enabled(not self.busy))

    # ---------- состояние ----------
    def has_text(self) -> bool:
        return bool(self.text.toPlainText().strip())

    def reset(self) -> None:
        self.text.clear()
        self.tr_text.clear()
        self._show_translation(False)
        self.status.setText("")
        self.status.setToolTip("")
        self.btn_open.hide()
        self.conv.hide()
        self._last_langs, self._url = [], ""

    def _show_translation(self, visible: bool) -> None:
        self.tr_text.setVisible(visible)
        self.btn_copy_tr.setVisible(visible)

    def _set_enabled(self, enabled: bool) -> None:
        has = self.has_text()
        self.btn_copy.setEnabled(enabled and has)
        self.btn_tr.setEnabled(enabled and has)
        self.btn_swap.setEnabled(enabled)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        if message:
            self.status.setText(message)
            self.status.setToolTip("")
        self._set_enabled(not busy)

    # ---------- распознавание ----------
    def recognize(self, image: QImage, then_translate: bool = False) -> None:
        if self.busy:
            return
        self._then_translate = then_translate
        self._show_translation(False)
        self._set_busy(True, "Распознаю…")
        run_async(self.ctx.ocr.recognize_qimage, self._on_ocr, image)

    def _on_ocr(self, text, error) -> None:
        self._set_busy(False)
        if error is not None:
            log.warning("OCR не выполнен: %s", error)
            self.status.setText(str(error) if isinstance(error, OcrError)
                                else "Не удалось распознать текст. Попробуйте выделить область ещё раз.")
            return
        if not text:
            self.status.setText("Текст не найден. Выделите область покрупнее или с более контрастным текстом.")
            return
        self.text.setPlainText(text)
        codes = getattr(self.ctx.ocr, "last_codes", []) or []
        self.btn_open.hide()
        self._url = ""
        if codes:                                    # на картинке QR/штрихкод — показываем его содержимое, а не буквы вокруг
            kinds = ", ".join(sorted({k for k, _ in codes}))
            self.status.setText(f"Найден код ({kinds}): показано его содержимое, а не текст вокруг. "
                                "Ссылки проверяйте перед открытием.")
            if len(codes) == 1 and re.fullmatch(r"https?://\S+", codes[0][1]):
                self._url = codes[0][1]
                self.btn_open.show()
        else:
            used = " + ".join(getattr(self.ctx.ocr, "last_used", []) or [])
            self.status.setText(f"Распознано ({used}). Текст можно исправить перед копированием или переводом."
                                if used else "Распознано. Текст можно исправить перед копированием или переводом.")
        self.recognized.emit(text)
        self._update_conv(text)
        if self._then_translate and not codes:
            self.translate_now()

    # ---------- кнопки ----------
    def _copy_text(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.status.setText("Текст скопирован в буфер обмена.")

    def _copy_translation(self) -> None:
        QGuiApplication.clipboard().setText(self.tr_text.toPlainText())
        self.status.setText("Перевод скопирован в буфер обмена.")

    def translate_now(self) -> None:
        src = self.text.toPlainText().strip()
        if not src or self.busy:
            return
        if self._failed:                    # ручной повтор после неудачи: пробуем все сервисы заново
            reset_cooldowns()
            self._failed = False
        self._set_busy(True, "Перевожу…")
        self._update_conv(src)
        run_async(translate, self._on_translated, src, self.lang.code(),
                  self.ctx.settings.get("translate_mode") or "auto")

    def reverse(self) -> None:
        """⇄ Меняет языки местами: перевод → в поле исходного текста, цель → язык оригинала."""
        if self.busy:
            return
        translated = self.tr_text.toPlainText().strip()
        source_text = self.text.toPlainText()
        origin = next((c for c in self._last_langs if self.lang.findData(c) >= 0), None)
        if origin is None:                                       # перевода ещё не было — берём язык текущего текста
            guess = detect_language(source_text) if source_text.strip() else "en"
            origin = guess if self.lang.findData(guess) >= 0 else "en"
        if translated:
            self.text.setPlainText(translated)
        self.tr_text.clear()
        self._show_translation(False)
        self._last_langs = []
        idx = self.lang.findData(origin)
        if idx >= 0:
            self.lang.setCurrentIndex(idx)
        self.status.setText(f"Языки поменяны: напишите или поправьте текст и нажмите «Перевести» — "
                            f"перевод будет на «{self.lang.currentText()}».")
        self.text.setFocus()

    def _update_conv(self, text: str) -> None:
        if text.strip():
            run_async(converters.describe, self._on_conv, text, self.ctx.settings.get("convert_currency") or "RUB")

    def _on_conv(self, lines, error) -> None:
        if error is not None or not lines:
            self.conv.hide()
            return
        self.conv.setText("Конвертер:\n" + "\n".join(lines))
        self.conv.show()

    def _open_url(self) -> None:
        if not self._url:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Открыть ссылку из QR-кода")
        box.setText(f"Открыть эту ссылку в браузере?\n\n{self._url}\n\n"
                    "Проверьте адрес: через QR-коды часто распространяют мошеннические сайты.")
        yes = box.addButton("Открыть", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            QDesktopServices.openUrl(QUrl(self._url))

    def _on_translated(self, result, error) -> None:
        self._set_busy(False)
        if error is not None:
            log.info("Перевод не выполнен: %s", error)
            self._failed = True
            details = getattr(error, "details", []) or []
            reasons = "; ".join(f"{name} — {why}" for name, why in details[:4])
            tip = ("\nСовет: скачайте офлайн-модель (Настройки → Распознавание и перевод) — она не зависит "
                   "от блокировок и работает быстрее." if any("скачана" in why or "пакет" in why
                                                              for _n, why in details) else "")
            self.status.setText("Не удалось перевести: сервисы недоступны. Проверьте интернет/VPN и повторите."
                                + (f"\nПричины: {reasons}" if reasons else "") + tip)
            self.status.setToolTip("\n".join(f"{n}: {w}" for n, w in details))
            return
        translated, engine, langs = result
        self._last_langs = list(langs)
        self.tr_text.setPlainText(translated)
        self._show_translation(True)
        if engine == "—":
            self.status.setText("Текст уже на выбранном языке — перевод не требуется.")
            self.tr_text.setPlainText(translated)
            return
        names = ", ".join(LANG_RU.get(code, code) for code in langs)
        self.status.setText(f"Переведено с: {names}. Сервис: {engine}." if names else f"Переведено. Сервис: {engine}.")


# =====================================================================================
#  Окно результата Alt+S
# =====================================================================================
class OcrResultWindow(QWidget):
    def __init__(self, ctx) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.ctx = ctx
        self.setObjectName("win")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Распознанный текст")
        self.resize(500, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        self.panel = TranslatePanel(ctx)
        lay.addWidget(self.panel)

    def process(self, image: QImage, rect: QRect) -> None:
        self.panel.reset()
        self._show_near(rect)
        self.panel.busy = False
        self.panel.recognize(image, then_translate=bool(self.ctx.settings.get("auto_translate")))

    def _show_near(self, rect: QRect) -> None:
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = min(max(rect.left(), geo.left() + 8), geo.right() - w - 8)
        y = rect.bottom() + 10
        if y + h > geo.bottom() - 8:
            y = max(geo.top() + 8, rect.top() - h - 10)
        self.move(x, y)
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)
