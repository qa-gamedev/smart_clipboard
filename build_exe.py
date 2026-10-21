"""Сборка SmartClipboard.exe через PyInstaller.   Запуск:  python build_exe.py   (или build_exe.bat)

Скрипт сам проверяет, какие из необязательных пакетов установлены, и подключает только их,
поэтому отсутствие, например, zxing-cpp не ломает сборку.
  python build_exe.py --dry-run     — только показать команду
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (модуль, обязателен ли) — для них добавляется --collect-all
COLLECT = [
    ("winrt.runtime", True), ("winrt.windows.foundation", True), ("winrt.windows.foundation.collections", True),
    ("winrt.windows.globalization", True), ("winrt.windows.graphics.imaging", True),
    ("winrt.windows.media.ocr", True), ("winrt.windows.storage.streams", True),
    ("winrt.windows.applicationmodel.datatransfer", False),
    ("ctranslate2", False), ("sentencepiece", False), ("zxingcpp", False), ("numpy", False),
    ("deep_translator", True), ("qrcode", True), ("requests", True),
]


def installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def build_command() -> tuple[list[str], list[str], list[str]]:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "SmartClipboard",
           "--exclude-module", "tkinter", "--hidden-import", "local_translate", "--hidden-import", "startup_import"]
    icon = HERE / "icon.ico"
    if icon.exists():
        cmd += ["--icon", str(icon)]
    missing_required, missing_optional = [], []
    for module, required in COLLECT:
        if installed(module):
            cmd += ["--collect-all", module]
        else:
            (missing_required if required else missing_optional).append(module)
    cmd.append(str(HERE / "main.py"))
    return cmd, missing_required, missing_optional


def main() -> int:
    dry = "--dry-run" in sys.argv
    print("=" * 60)
    print("  Сборка SmartClipboard.exe (PyInstaller)")
    print("=" * 60)
    if not dry:
        print("Ставлю/обновляю PyInstaller...")
        if subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"]) != 0:
            print("Не удалось установить PyInstaller (проверьте интернет).")
            return 1
        print("Рисую иконку...")
        subprocess.call([sys.executable, str(HERE / "make_icon.py")], cwd=HERE)
    cmd, missing_required, missing_optional = build_command()
    if missing_required:
        print("\nНЕ УСТАНОВЛЕНЫ обязательные пакеты:", ", ".join(missing_required))
        print("Выполните:  pip install -r requirements.txt   и запустите сборку снова.")
        if not dry:
            return 1
    if missing_optional:
        print("Необязательные пакеты не найдены (функции будут отключены в exe):", ", ".join(missing_optional))
    print("\nКоманда:", " ".join(f'"{c}"' if " " in c else c for c in cmd), "\n")
    if dry:
        return 0
    code = subprocess.call(cmd, cwd=HERE)
    if code != 0:
        print("\nСборка не удалась. Скопируйте текст ошибки выше.")
        return code
    print("\nГОТОВО:  dist\\SmartClipboard\\SmartClipboard.exe")
    print("Запустите его один раз и включите в Настройках «Запускать вместе с Windows» —")
    print("в автозагрузку запишется путь к этому exe. Папку dist\\SmartClipboard можно переносить целиком.")
    print("Если чего-то не хватает, ошибка будет в %APPDATA%\\ClipboardManager\\app.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
