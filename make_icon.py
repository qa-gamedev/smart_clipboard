"""Создаёт icon.ico для сборки exe (рисуется тем же кодом, что и значок в трее)."""
import sys
from pathlib import Path

from PyQt6.QtGui import QGuiApplication

sys.path.insert(0, str(Path(__file__).resolve().parent))

app = QGuiApplication(sys.argv)
from ui_main import make_icon  # noqa: E402  (нужен QGuiApplication)

pixmap = make_icon().pixmap(256, 256)
print("icon.ico создан" if pixmap.save("icon.ico") else "не удалось сохранить icon.ico")
