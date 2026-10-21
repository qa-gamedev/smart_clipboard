"""Дополнительные окна: редактор заметок, теги и цвета, диагностика OCR, сохранение в Word."""
from __future__ import annotations

import difflib
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QStandardPaths, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

import docx_writer
import winutil
from ocr_translator import run_async
from theme import TINTS

log = logging.getLogger(__name__)

# (ключ, название, цвет) — ключ хранится в базе и в QSS (#card[tint="red"])
COLORS = [("", "Без цвета", "#00000000")] + list(TINTS)

_windows: set = set()


def keep_open(window: QWidget) -> None:
    """Окна без родителя иначе удалит сборщик мусора."""
    _windows.add(window)
    window.destroyed.connect(lambda *_: _windows.discard(window))


def swatch_icon(hex_color: str, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor("#555b66"))
    p.setBrush(QColor(hex_color) if hex_color != "#00000000" else Qt.BrushStyle.NoBrush)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def color_combo(current: str = "") -> QComboBox:
    combo = QComboBox()
    for key, title, hex_color in COLORS:
        combo.addItem(swatch_icon(hex_color), title, key)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    return combo


def text_stats(text: str) -> str:
    words = len(re.findall(r"\w+", text, flags=re.UNICODE))
    lines = text.count("\n") + 1 if text else 0
    return f"{len(text):,} симв. · {words:,} слов · {lines:,} строк".replace(",", " ")


# =====================================================================================
#  Сохранение в Word
# =====================================================================================
def save_to_word(text: str, parent=None) -> str | None:
    """Диалог «Сохранить как» (по умолчанию — рабочий стол) и запись .docx. Возвращает путь или None."""
    desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation) or str(Path.home())
    suggested = Path(desktop) / f"Текст_{datetime.now():%Y-%m-%d_%H-%M}.docx"
    path, _ = QFileDialog.getSaveFileName(parent, "Сохранить в Word", str(suggested), "Документ Word (*.docx)")
    if not path:
        return None
    if not path.lower().endswith(".docx"):
        path += ".docx"
    try:
        docx_writer.write_docx(path, text)
    except OSError as e:
        QMessageBox.warning(parent, "Сохранение в Word", f"Не удалось сохранить файл:\n{e}")
        return None
    box = QMessageBox(parent)
    box.setWindowTitle("Сохранение в Word")
    box.setText(f"Документ сохранён:\n{path}")
    open_btn = box.addButton("Открыть", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    box.exec()
    if box.clickedButton() is open_btn:
        try:
            os.startfile(path)                                # noqa: S606 — открыть в Word
        except OSError:
            pass
    return path


# =====================================================================================
#  Редактор заметок (и правка скопированного текста)
# =====================================================================================
class NoteEditor(QWidget):
    saved = pyqtSignal(int)

    def __init__(self, ctx, row: dict | None = None) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.ctx, self.row = ctx, row
        self.item_id = row["id"] if row else None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("win")
        is_note = bool(row and row["type"] == "note") or row is None
        self.setWindowTitle("Новая заметка" if row is None else ("Заметка" if is_note else "Редактирование текста"))
        self.setMinimumSize(520, 420)
        initial = ctx.db.full_text(row) if row else ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.tags = QLineEdit(row["tags"] if row else "")
        self.tags.setPlaceholderText("#теги через пробел — по ним потом удобно искать")
        self.color = color_combo(row["color"] if row else "")
        top.addWidget(self.tags, 1)
        top.addWidget(self.color)
        lay.addLayout(top)

        self.edit = QPlainTextEdit(initial)
        self.edit.setPlaceholderText("Текст заметки…")
        lay.addWidget(self.edit, 1)

        bottom = QHBoxLayout()
        self.stats = QLabel()
        self.stats.setObjectName("muted")
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.b_copy = QPushButton("Копировать")
        self.b_word = QPushButton("В Word…")
        self.b_save = QPushButton("Сохранить")
        self.b_save.setObjectName("primary")
        self.b_save.setToolTip("Ctrl+S")
        for b in (self.b_copy, self.b_word, self.b_save):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bottom.addWidget(self.stats)
        bottom.addWidget(self.status)
        bottom.addStretch(1)
        bottom.addWidget(self.b_copy)
        bottom.addWidget(self.b_word)
        bottom.addWidget(self.b_save)
        lay.addLayout(bottom)

        self._dirty = False
        self._update_stats()
        self.edit.textChanged.connect(self._changed)
        self.tags.textChanged.connect(self._changed)
        self.color.currentIndexChanged.connect(self._changed)
        self.b_copy.clicked.connect(self._copy)
        self.b_word.clicked.connect(lambda: save_to_word(self.edit.toPlainText(), self))
        self.b_save.clicked.connect(self.save)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.resize(640, 520)
        self.move(geo.center() - self.rect().center())

    def open(self) -> None:
        keep_open(self)
        self.show()
        self.raise_()
        winutil.force_foreground(int(self.winId()))
        self.activateWindow()
        self.edit.setFocus()

    def _update_stats(self) -> None:
        self.stats.setText(text_stats(self.edit.toPlainText()))

    def _changed(self) -> None:
        self._dirty = True
        self.status.setText("")
        self._update_stats()

    def _copy(self) -> None:
        self.ctx.monitor.ignore_next()
        QGuiApplication.clipboard().setText(self.edit.toPlainText())
        self.status.setText("Скопировано ✓")

    def save(self) -> bool:
        text = self.edit.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Заметка", "Пустую заметку сохранять не нужно.")
            return False
        db = self.ctx.db
        try:
            if self.item_id is None:
                self.item_id = db.add_note(text, self.tags.text(), self.color.currentData())
            else:
                self.item_id = db.update_text(self.item_id, text)
                db.set_meta(self.item_id, tags=self.tags.text(), color=self.color.currentData())
        except Exception:
            log.exception("Не удалось сохранить заметку")
            QMessageBox.warning(self, "Заметка", "Не удалось сохранить. Подробности в app.log.")
            return False
        self._dirty = False
        self.status.setText("Сохранено ✓")
        self.saved.emit(self.item_id)
        self.ctx.monitor.changed.emit()                       # обновит список в окне истории
        return True

    def closeEvent(self, e) -> None:
        if self._dirty and self.edit.toPlainText().strip():
            box = QMessageBox(self)
            box.setWindowTitle("Заметка")
            box.setText("Сохранить изменения?")
            yes = box.addButton("Сохранить", QMessageBox.ButtonRole.AcceptRole)
            no = box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is yes and not self.save():
                e.ignore()
                return
            if clicked not in (yes, no):
                e.ignore()
                return
        super().closeEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


# =====================================================================================
#  Теги и цвет карточки
# =====================================================================================
class TagDialog(QDialog):
    def __init__(self, ctx, row: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Теги и цвет")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 14)
        lay.setSpacing(10)
        self.tags = QLineEdit(row.get("tags") or "")
        self.tags.setPlaceholderText("#работа #идея — через пробел")
        self.color = color_combo(row.get("color") or "")
        lay.addWidget(QLabel("Теги (по ним можно фильтровать и искать):"))
        lay.addWidget(self.tags)
        existing = ctx.db.all_tags()[:12]
        if existing:
            hint = QLabel("Уже есть — нажмите, чтобы добавить:")
            hint.setObjectName("muted")
            lay.addWidget(hint)
            chips = QHBoxLayout()
            chips.setSpacing(4)
            for tag, count in existing:
                b = QPushButton(f"{tag} ({count})")
                b.setObjectName("chip")
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(lambda _c=False, t=tag: self._add(t))
                chips.addWidget(b)
            chips.addStretch(1)
            lay.addLayout(chips)
        lay.addWidget(QLabel("Цвет карточки:"))
        lay.addWidget(self.color)
        ok, cancel = QPushButton("Сохранить"), QPushButton("Отмена")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

    def _add(self, tag: str) -> None:
        current = self.tags.text().split()
        if tag not in current:
            self.tags.setText(" ".join(current + [tag]))

    def values(self) -> tuple[str, str]:
        return self.tags.text(), self.color.currentData()


# =====================================================================================
#  Диагностика распознавания: какой движок что читает и насколько точно
# =====================================================================================
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class OcrDiagDialog(QDialog):
    EXPECTED = ["Hello world, 12345 — The quick brown fox",
                "Привет, мир! Быстрая коричневая лиса",
                "안녕하세요 반갑습니다"]

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Проверка распознавания")
        self.setMinimumSize(620, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        self.status = QLabel("Рисую тестовую картинку и запускаю движки по очереди…")
        self.status.setWordWrap(True)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        lay.addWidget(self.status)
        lay.addWidget(self.out, 1)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        run_async(self.ctx.ocr.diagnose, self._done, self._test_image())

    def _test_image(self):
        from PyQt6.QtGui import QFont, QImage
        img = QImage(1000, 330, QImage.Format.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setPen(QColor("#111111"))
        font = QFont("Segoe UI")
        font.setPixelSize(30)
        p.setFont(font)
        for i, line in enumerate(self.EXPECTED):
            p.drawText(30, 70 + i * 70, line)
        p.end()
        return img

    def _done(self, results, error) -> None:
        if error is not None:
            self.status.setText(f"Проверка не удалась: {error}")
            return
        expected = _norm(" ".join(self.EXPECTED))
        blocks, best = [], None
        for r in results:
            if r["error"]:
                blocks.append(f"■ {r['engine']} — ошибка: {r['error']}")
                continue
            ratio = difflib.SequenceMatcher(None, expected, _norm(r["text"])).ratio()
            best = max(best or (0, ""), (ratio, r["engine"]))
            blocks.append(f"■ {r['engine']} — совпадение с эталоном {ratio * 100:.0f}%, {r['seconds']:.1f} с\n"
                          + (r["text"] or "(ничего не распознано)"))
        try:
            langs = ", ".join(tag for tag, _ in self.ctx.ocr.languages()) or "нет"
        except Exception:
            langs = "не удалось получить"
        head = "Эталон:\n" + "\n".join(self.EXPECTED) + f"\n\nЯзыки Windows OCR: {langs}\n" + "─" * 50
        self.out.setPlainText(head + "\n\n" + "\n\n".join(blocks))
        names = {r["engine"] for r in results if not r["error"]}
        self.status.setText(
            "Лучший результат: " + (f"{best[1]} ({best[0] * 100:.0f}%)" if best else "—")
            + ". Подключены: " + (", ".join(sorted(names)) or "ничего") + ". "
            + ("Tesseract работает." if "Tesseract" in names else "Tesseract не найден или не установлены его языки."))
