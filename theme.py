"""Единый тёмный стиль (QSS) для всех окон приложения."""

ACCENT = "#5b8def"

# Цветовые метки карточек: (ключ в базе, название, цвет)
TINTS = [("red", "Красный", "#e5484d"), ("orange", "Оранжевый", "#f76b15"), ("yellow", "Жёлтый", "#e5b800"),
         ("green", "Зелёный", "#30a46c"), ("teal", "Бирюзовый", "#12a594"), ("blue", "Синий", "#5b8def"),
         ("purple", "Фиолетовый", "#9b6bff"), ("pink", "Розовый", "#e93d82")]

QSS = """
QWidget { color:#e6e8ec; font-family:'Segoe UI'; font-size:13px; }
#root { background:#1d2026; border:1px solid #3a404b; border-radius:12px; }
#root[secret="true"] { border:1px solid #9b6bff; }
#win, QDialog, QMessageBox { background:#1d2026; }

QLineEdit, QPlainTextEdit, QComboBox, QKeySequenceEdit {
    background:#272b33; border:1px solid #3a404b; border-radius:8px;
    padding:6px 9px; selection-background-color:#3d6fd9;
}
QLineEdit:focus, QPlainTextEdit:focus, QKeySequenceEdit:focus { border-color:#5b8def; }
QComboBox::drop-down { border:none; width:22px; }
QComboBox QAbstractItemView {
    background:#272b33; border:1px solid #3a404b; outline:0;
    selection-background-color:#3d6fd9;
}

QPushButton {
    background:#2f343e; border:1px solid #3a404b; border-radius:8px; padding:6px 12px;
}
QPushButton:hover { background:#383e4a; }
QPushButton:checked { background:#3d6fd9; border-color:#3d6fd9; color:white; }
QPushButton:disabled { color:#7b818d; }
QPushButton#chip { padding:5px 8px; font-size:12px; }
QPushButton#primary { background:#3d6fd9; border-color:#3d6fd9; color:white; }
QPushButton#primary:hover { background:#4a7be6; }

QToolButton#mini {
    background:transparent; border:none; border-radius:6px; padding:2px 7px; color:#aeb4c0;
}
QToolButton#mini:hover { background:#3a404b; color:white; }

#card { background:#272b33; border:1px solid transparent; border-radius:10px; }
#card:hover { border-color:#434a57; }
#card[sel="true"] { border-color:#5b8def; background:#2d3442; }

QListWidget { background:transparent; border:none; outline:0; }
QListWidget::item { background:transparent; border:none; }
QListWidget::item:selected { background:transparent; }

QScrollBar:vertical { background:transparent; width:8px; margin:0; }
QScrollBar::handle:vertical { background:#454c59; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }

QLabel#muted { color:#8e95a3; font-size:11px; }
QLabel#conv { color:#9fc2ff; background:#232a38; border:1px solid #33405a; border-radius:8px; padding:8px; }
QLabel#badge { color:#c3a9ff; font-size:11px; }
QTabWidget::pane { border:1px solid #3a404b; border-radius:8px; top:-1px; }
QTabBar::tab { background:#23272e; border:1px solid #3a404b; padding:6px 12px; margin-right:2px;
               border-top-left-radius:8px; border-top-right-radius:8px; }
QTabBar::tab:selected { background:#2f343e; color:white; }
QProgressBar { background:#272b33; border:1px solid #3a404b; border-radius:6px; text-align:center; height:14px; }
QProgressBar::chunk { background:#3d6fd9; border-radius:5px; }
QSpinBox { background:#272b33; border:1px solid #3a404b; border-radius:8px; padding:5px 8px; }
QToolTip { background:#15181d; color:#e6e8ec; border:1px solid #3a404b; padding:3px 6px; }
QMenu { background:#272b33; border:1px solid #3a404b; padding:4px; }
QMenu::item { padding:6px 22px; border-radius:6px; }
QMenu::item:selected { background:#3d6fd9; }
QMenu::separator { height:1px; background:#3a404b; margin:4px 6px; }
QCheckBox { spacing:8px; }
"""

QSS += "".join(f'#card[tint="{k}"] {{ border-left:3px solid {c}; }}\n' for k, _t, c in TINTS)
