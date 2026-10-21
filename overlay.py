"""Перевод поверх экрана: инструмент сканирует выбранную область (или весь экран) и накладывает
перевод прямо поверх исходного текста. Подходит для игр, видео, программ без выделяемого текста.

Как устроено (и почему не нагружает ПК)
* Окно-наложение пропускает клики сквозь себя везде, кроме плашки управления и блоков перевода:
  плашку можно перетаскивать (область переезжает вместе с ней), клик по блоку копирует перевод,
  правый клик — оригинал, есть «пауза» и «копировать всё». Окно исключено из снимков экрана
  (SetWindowDisplayAffinity), поэтому OCR не читает собственный перевод.
* Экран проверяется по таймеру (по умолчанию раз в 1,2 с), но дорогое распознавание запускается
  ТОЛЬКО когда картинка действительно изменилась (сравнение уменьшенных копий кадра).
* Переводы кэшируются: повторяющийся текст (интерфейс игры, реплики) не переводится заново.
* Для скорости лучше всего скачать офлайн-модель (Настройки → Распознавание и перевод).
Ограничения: игры в «эксклюзивном полноэкранном» режиме перекрывают любые окна — переключите игру
в режим «окно без рамки».
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QImage, QPainter, QPen, QPolygon,
                         QRegion)
from PyQt6.QtWidgets import QWidget

import winutil
from ocr_translator import ScreenGrabber, run_async
from translator import translate_lines

log = logging.getLogger(__name__)

WRAP = Qt.TextFlag.TextWordWrap.value                                       # в PyQt6 флаги нужно передавать как .value
ALIGN = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value
MIN_INTERVAL_MS = 500
SAME_FRAME_DIFF = 2.0      # средняя разница яркости (0–255): меньше — кадр «тот же»
NEW_SCENE_DIFF = 18.0      # больше — сцена сменилась, старый перевод убираем сразу


class OverlayWindow(QWidget):
    """Окно поверх области. Клики проходят сквозь него везде, кроме «плашки» управления и блоков перевода
    (это достигается маской окна — SetWindowRgn, поэтому игра под наложением продолжает получать мышь)."""
    moved_by_user = pyqtSignal()
    pause_clicked = pyqtSignal()
    copy_all_clicked = pyqtSignal()
    close_clicked = pyqtSignal()
    block_clicked = pyqtSignal(int, bool)         # (индекс блока, копировать оригинал?)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.capture_excluded = False
        self.dragging = False
        self._items: list[tuple[QRect, str, QColor, QColor]] = []
        self._boxes: list[tuple[QRect, QFont]] = []
        self._status = ""
        self._paused = False
        self._drag = None
        self._flash = -1
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._unflash)

    # ---------- показ ----------
    def place(self, rect: QRect) -> None:
        self.setGeometry(rect)
        self.show()
        self._exclude()
        QTimer.singleShot(300, self._exclude)           # на случай, если окно создалось не сразу

    def _exclude(self) -> None:
        if not self.capture_excluded:
            self.capture_excluded = winutil.exclude_from_capture(int(self.winId()))
            if not self.capture_excluded:
                log.info("Окно не исключено из снимков — на время снимка оно будет скрываться")

    def set_items(self, items, status: str = "") -> None:
        self._items = items
        self._status = status
        self._relayout()
        self.update()

    def set_status(self, status: str) -> None:
        self._status = status
        self._relayout()
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._relayout()
        self.update()

    # ---------- геометрия ----------
    @staticmethod
    def _font(size: int) -> QFont:
        f = QFont("Segoe UI")
        f.setPixelSize(size)
        return f

    def _fit(self, text: str, w: int, h: int) -> tuple[QFont, int]:
        """Самый крупный шрифт, при котором перевод помещается; иначе — минимальный и нужная высота."""
        size = max(9, min(40, int(h * 0.8)))
        while True:
            font = self._font(size)
            need = QFontMetrics(font).boundingRect(QRect(0, 0, max(1, w), 10000), WRAP, text).height()
            if need <= h or size <= 9:
                return font, need
            size -= 1

    def _badge(self):
        text = ("Пауза · " if self._paused else "") + self._status
        width = QFontMetrics(self._font(11)).horizontalAdvance(text) + 14 if text else 0
        x, y, h = 6, 6, 22
        handle = QRect(x, y, 22, h)
        x += 22
        label = QRect(x, y, width, h)
        x += width
        pause, copy, close = QRect(x, y, 24, h), QRect(x + 24, y, 24, h), QRect(x + 48, y, 24, h)
        pill = QRect(6, y, x + 72 - 6, h)
        return pill, handle, label, pause, copy, close, text

    def _relayout(self) -> None:
        self._boxes = []
        for rect, text, _bg, _fg in self._items:
            box = rect.adjusted(-3, -2, 3, 2)
            font, need = self._fit(text, box.width() - 8, box.height() - 4)
            box.setHeight(min(max(box.height(), need + 6), box.height() * 3))
            self._boxes.append((box.intersected(self.rect()), font))
        w, h = self.width(), self.height()
        if w < 4 or h < 4:
            return
        region = QRegion(self._badge()[0])
        for box, _f in self._boxes:
            region = region.united(QRegion(box))
        for strip in (QRect(0, 0, w, 1), QRect(0, h - 1, w, 1), QRect(0, 0, 1, h), QRect(w - 1, 0, 1, h)):
            region = region.united(QRegion(strip))      # тонкая рамка области
        self.setMask(region)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._relayout()

    # ---------- рисование ----------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(QPen(QColor(91, 141, 239, 150), 1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        for i, ((box, font), (_rect, text, bg, fg)) in enumerate(zip(self._boxes, self._items)):
            p.setPen(QPen(QColor("#5b8def"), 2) if i == self._flash else Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(box, 4, 4)
            p.setFont(font)
            p.setPen(fg)
            p.drawText(box.adjusted(4, 2, -4, -2), ALIGN | WRAP, text)
        pill, handle, label, pause, copy, close, text = self._badge()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 24, 31, 225))
        p.drawRoundedRect(pill, 6, 6)
        ink = QColor(220, 224, 232)
        p.setPen(QPen(ink, 1.6))
        cx, cy = handle.center().x(), handle.center().y()
        for dy in (-4, 0, 4):                                        # ручка перетаскивания
            p.drawLine(cx - 5, cy + dy, cx + 5, cy + dy)
        if text:
            p.setFont(self._font(11))
            p.drawText(label.adjusted(4, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter.value), text)
        p.setBrush(ink)
        p.setPen(Qt.PenStyle.NoPen)
        pc = pause.center()
        if self._paused:                                              # ▶
            p.drawPolygon(QPolygon([QPoint(pc.x() - 3, pc.y() - 6), QPoint(pc.x() - 3, pc.y() + 6),
                                    QPoint(pc.x() + 6, pc.y())]))
        else:                                                         # ⏸
            p.drawRect(pc.x() - 5, pc.y() - 6, 3, 12)
            p.drawRect(pc.x() + 2, pc.y() - 6, 3, 12)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(ink, 1.5))
        cc = copy.center()
        p.drawRoundedRect(cc.x() - 6, cc.y() - 3, 8, 9, 2, 2)         # ⧉
        p.drawRoundedRect(cc.x() - 2, cc.y() - 7, 8, 9, 2, 2)
        xc = close.center()
        p.drawLine(xc.x() - 4, xc.y() - 4, xc.x() + 4, xc.y() + 4)    # ✕
        p.drawLine(xc.x() - 4, xc.y() + 4, xc.x() + 4, xc.y() - 4)

    # ---------- мышь ----------
    def mousePressEvent(self, e) -> None:
        pos = e.position().toPoint()
        pill, _handle, _label, pause, copy, close, _t = self._badge()
        if e.button() == Qt.MouseButton.LeftButton:
            if pause.contains(pos):
                self.pause_clicked.emit()
                return
            if copy.contains(pos):
                self.copy_all_clicked.emit()
                return
            if close.contains(pos):
                self.close_clicked.emit()
                return
            if pill.contains(pos):                                    # тянем область за плашку
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self.dragging = True
                return
        for i, (box, _f) in enumerate(self._boxes):
            if box.contains(pos):
                self.block_clicked.emit(i, e.button() == Qt.MouseButton.RightButton)
                self._flash = i
                self.update()
                self._flash_timer.start(350)
                return

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag is not None:
            self._drag = None
            self.dragging = False
            self.moved_by_user.emit()

    def _unflash(self) -> None:
        self._flash = -1
        self.update()


def _bg_color(img: QImage, x: float, y: float, w: float, h: float) -> QColor:
    """Фон под текстом: усредняем пиксели вокруг строки (сам текст в выборку не попадает)."""
    x, y, w, h = int(x), int(y), int(w), int(h)
    pts = [(x - 3, y + h // 2), (x + w + 3, y + h // 2), (x + w // 2, y - 3), (x + w // 2, y + h + 3),
           (x - 3, y - 3), (x + w + 3, y + h + 3)]
    cols = [img.pixelColor(px, py) for px, py in pts if 0 <= px < img.width() and 0 <= py < img.height()]
    if not cols:
        return QColor(24, 26, 31, 235)
    n = len(cols)
    return QColor(sum(c.red() for c in cols) // n, sum(c.green() for c in cols) // n,
                  sum(c.blue() for c in cols) // n, 238)


def _fg_for(bg: QColor) -> QColor:
    lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return QColor(255, 255, 255) if lum < 140 else QColor(18, 18, 22)


class OverlayController(QObject):
    state_changed = pyqtSignal(bool)

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.window = OverlayWindow()
        self.grabber = ScreenGrabber()
        self.grabber.captured.connect(self._on_region)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.active = False
        self._rect: QRect | None = None
        self._screen = None
        self._busy = False
        self._sig: bytes | None = None
        self._gen = 0
        self._paused = False
        self._blocks: list[tuple[str, str]] = []             # (перевод, оригинал) для каждого блока на экране
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.window.set_status(self._status_text()))
        w = self.window
        w.pause_clicked.connect(self.toggle_pause)
        w.copy_all_clicked.connect(self.copy_all)
        w.close_clicked.connect(self.stop)
        w.block_clicked.connect(self.copy_block)
        w.moved_by_user.connect(self._on_moved)

    # ---------- запуск / остановка ----------
    def toggle_region(self) -> None:
        """Горячая клавиша: если включено — выключить, иначе выбрать область."""
        if self.active:
            self.stop()
        else:
            self.grabber.start()

    def start_fullscreen(self) -> None:
        from PyQt6.QtGui import QCursor
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self._begin(screen.geometry())

    def _on_region(self, _image, rect: QRect) -> None:
        self._begin(rect)

    def _begin(self, rect: QRect) -> None:
        self._rect = QRect(rect)
        self._screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        self._sig = None
        self._busy = False
        self._gen += 1
        self._paused = False
        self._blocks = []
        self.window.set_paused(False)
        self.window.set_items([], self._status_text())
        self.window.place(self._rect)
        self.active = True
        self.reload_settings()
        self.state_changed.emit(True)
        QTimer.singleShot(150, self._tick)

    def stop(self) -> None:
        self._gen += 1
        self._timer.stop()
        self.window.hide()
        self.active = False
        self._busy = False
        self.state_changed.emit(False)

    # ---------- управление с плашки ----------
    def _status_text(self) -> str:
        return f"Перевод: {len(self._blocks)}" if self._blocks else "Перевод включён"

    def toggle_pause(self) -> None:
        """Пауза: сканирование стоит, перевод остаётся на экране — можно спокойно читать и копировать."""
        self._paused = not self._paused
        self.window.set_paused(self._paused)
        if not self._paused:
            self._sig = None                              # сняли паузу — сразу перечитать экран
            QTimer.singleShot(100, self._tick)

    def _flash_status(self, text: str) -> None:
        self.window.set_status(text)
        self._status_timer.start(1200)

    def copy_block(self, index: int, original: bool) -> None:
        if 0 <= index < len(self._blocks):
            QGuiApplication.clipboard().setText(self._blocks[index][1 if original else 0])
            self._flash_status("Оригинал скопирован ✓" if original else "Перевод скопирован ✓")

    def copy_all(self) -> None:
        if self._blocks:
            QGuiApplication.clipboard().setText("\n".join(tr for tr, _orig in self._blocks))
            self._flash_status("Весь перевод скопирован ✓")

    def _on_moved(self) -> None:
        """Область перетащили: дальше сканируем новое место."""
        self._rect = QRect(self.window.geometry())
        self._screen = QGuiApplication.screenAt(self._rect.center()) or QGuiApplication.primaryScreen()
        self._gen += 1
        self._busy = False
        self._sig = None
        self._blocks = []
        self.window.set_items([], self._status_text())

    def reload_settings(self) -> None:
        interval = max(MIN_INTERVAL_MS, int(self.ctx.settings.get("overlay_interval_ms") or 1200))
        if self.active:
            self._timer.start(interval)

    # ---------- цикл ----------
    def _grab(self) -> QImage | None:
        geo = self._screen.geometry()
        hide = self.window.isVisible() and not self.window.capture_excluded
        if hide:                                          # запасной путь для старых Windows
            self.window.hide()
            QGuiApplication.processEvents()
        pm = self._screen.grabWindow(0, self._rect.x() - geo.x(), self._rect.y() - geo.y(),
                                     self._rect.width(), self._rect.height())
        if hide:
            self.window.show()
        img = pm.toImage()
        self._dpr = pm.devicePixelRatio() or 1.0
        return None if img.isNull() else img

    @staticmethod
    def _signature(img: QImage) -> bytes:
        small = img.scaled(48, 28, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.FastTransformation).convertToFormat(QImage.Format.Format_Grayscale8)
        n = small.sizeInBytes()
        ptr = small.constBits()
        ptr.setsize(n)
        return ptr.asstring(n)

    @staticmethod
    def _diff(a: bytes, b: bytes) -> float:
        return sum(abs(x - y) for x, y in zip(a, b)) / max(1, min(len(a), len(b)))

    def _tick(self) -> None:
        if not self.active or self._busy or self._paused or self._screen is None or self.window.dragging:
            return
        img = self._grab()
        if img is None:
            return
        sig = self._signature(img)
        diff = self._diff(sig, self._sig) if self._sig is not None else 99.0
        if diff < SAME_FRAME_DIFF:
            return                                        # экран не менялся — OCR не запускаем
        if diff > NEW_SCENE_DIFF:
            self._blocks = []
            self.window.set_items([], self._status_text())  # сцена сменилась — убираем устаревший текст
        self._sig = sig
        self._busy = True
        gen, dpr = self._gen, self._dpr
        run_async(self._job, lambda res, err: self._done(res, err, gen, dpr), img)

    def _job(self, img: QImage):
        lines = [ln for ln in self.ctx.ocr.recognize_lines(img) if sum(c.isalpha() for c in ln[0]) >= 2]
        if not lines:
            return [], [], []
        s = self.ctx.settings
        translations = translate_lines([ln[0] for ln in lines], s.get("translate_to") or "ru",
                                       s.get("translate_mode") or "auto")
        colors = [_bg_color(img, x, y, w, h) for _t, x, y, w, h in lines]
        return lines, translations, colors

    def _done(self, res, err, gen: int, dpr: float) -> None:
        self._busy = False
        if gen != self._gen or not self.active:
            return
        if err is not None:
            log.info("Оверлей: %s", err)
            self._blocks = []
            self.window.set_items([], "Нет перевода — скачайте офлайн-модель в настройках")
            return
        lines, translations, colors = res
        items, blocks = [], []
        for (text, x, y, w, h), tr, bg in zip(lines, translations, colors):
            if not tr.strip() or tr.strip() == text.strip():
                continue                                  # уже на нужном языке
            items.append((QRect(int(x / dpr), int(y / dpr), max(8, int(w / dpr)), max(8, int(h / dpr))),
                          tr, bg, _fg_for(bg)))
            blocks.append((tr, text))
        self._blocks = blocks
        self.window.set_items(items, self._status_text())
