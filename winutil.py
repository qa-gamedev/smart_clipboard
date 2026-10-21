"""Win32-утилиты на чистом ctypes: глобальные хоткеи, фокус окон,
эмуляция Ctrl+V, автозапуск, приоритеты потоков.

Хоткеи сделаны через RegisterHotKey в отдельном потоке, который просто спит
в GetMessage: ноль нагрузки на CPU (в отличие от клавиатурных хуков pynput/keyboard).
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
shell32 = ctypes.WinDLL("shell32")

WM_QUIT, WM_USER, WM_HOTKEY = 0x0012, 0x0400, 0x0312
PM_NOREMOVE = 0
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

# --- прототипы (важно для 64-битных указателей) ---------------------------------
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]


# --- разбор строки хоткея ------------------------------------------------------------
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN, "meta": MOD_WIN}
_SPECIAL = {"space": 0x20, "tab": 0x09, "ins": 0x2D, "insert": 0x2D, "del": 0x2E, "delete": 0x2E,
            "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdown": 0x22}
# Кириллица → латиница по физическому положению клавиши (для хоткеев в русской раскладке)
_RU = {}
for _ru, _en in (("йцукенгшщз", "qwertyuiop"), ("фывапролд", "asdfghjkl"), ("ячсмить", "zxcvbnm")):
    _RU.update(zip(_ru, _en))


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Alt+V' -> (модификаторы, виртуальный код). ValueError при ошибке."""
    mods, vk = 0, 0
    for part in (p.strip().lower() for p in text.split("+")):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
            continue
        part = _RU.get(part, part)
        if len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
        elif part in _SPECIAL:
            vk = _SPECIAL[part]
        else:
            raise ValueError(f"неизвестная клавиша «{part}»")
    if not vk:
        raise ValueError("не указана основная клавиша")
    if not mods:
        raise ValueError("нужен модификатор (Alt, Ctrl, Shift или Win)")
    return mods, vk


class HotkeyThread(QThread):
    """Глобальные хоткеи. Поток блокируется в GetMessage — CPU не тратится."""
    pressed = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, hotkeys: dict[int, str], parent=None) -> None:
        super().__init__(parent)
        self._hotkeys = dict(hotkeys)
        self._tid = 0
        self._ready = threading.Event()

    def run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)  # создаёт очередь потока
        registered = []
        for hid, text in self._hotkeys.items():
            try:
                mods, vk = parse_hotkey(text)
                if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                    code = ctypes.get_last_error()
                    reason = "уже занято другой программой" if code == 1409 else f"ошибка {code}"
                    raise OSError(reason)
                registered.append(hid)
            except Exception as e:
                self.failed.emit(f"Не удалось назначить {text}: {e}")
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.pressed.emit(int(msg.wParam))
        for hid in registered:
            user32.UnregisterHotKey(None, hid)

    def stop(self) -> None:
        if self.isRunning():
            self._ready.wait(2)
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self.wait(2000)


# --- фокус окон и вставка ---------------------------------------------------------------
def foreground_window():
    return user32.GetForegroundWindow()


def is_window(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def force_foreground(hwnd) -> None:
    """Надёжно переводит окно на передний план (обход ограничения Windows на кражу фокуса)."""
    try:
        fg = user32.GetForegroundWindow()
        if not hwnd or fg == hwnd:
            return
        cur = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attach = bool(fg_tid) and fg_tid != cur
        if attach:
            user32.AttachThreadInput(fg_tid, cur, True)
        user32.SetForegroundWindow(hwnd)
        if attach:
            user32.AttachThreadInput(fg_tid, cur, False)
    except Exception:
        log.debug("force_foreground failed", exc_info=True)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send_ctrl_v() -> None:
    """Эмулирует нажатие Ctrl+V в активном окне."""
    def ev(vk: int, up: bool = False) -> _INPUT:
        i = _INPUT()
        i.type = 1  # INPUT_KEYBOARD
        i.ki.wVk = vk
        i.ki.dwFlags = 2 if up else 0  # KEYEVENTF_KEYUP
        return i
    seq = (_INPUT * 4)(ev(0x11), ev(0x56), ev(0x56, True), ev(0x11, True))
    user32.SendInput(4, seq, ctypes.sizeof(_INPUT))


# --- автозапуск, процесс, потоки -----------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SmartClipboardManager"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")  # без консольного окна
    script = Path(__file__).with_name("main.py").resolve()
    return f'"{pyw if pyw.exists() else py}" "{script}"'


def set_autostart(enabled: bool) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        log.warning("Не удалось изменить автозапуск", exc_info=True)


_mutex = None


def already_running(name: str = "Local\\SmartClipboardManager") -> bool:
    """Защита от запуска второй копии."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def set_app_id() -> None:
    try:
        shell32.SetCurrentProcessExplicitAppUserModelID("SmartClipboardManager")
    except Exception:
        pass


def lower_current_thread_priority() -> None:
    """Фоновые потоки (OCR, сохранение картинок) не мешают остальным программам."""
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)  # BELOW_NORMAL
    except Exception:
        pass


def init_com_mta() -> None:
    try:
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED — нужно для WinRT в рабочем потоке
    except Exception:
        pass


def trim_memory() -> None:
    """Сбрасывает неиспользуемую память процесса (меньше RAM в простое)."""
    try:
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(),
                                          ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass


def exclude_from_capture(hwnd) -> bool:
    """Окно не попадает в снимки экрана (Windows 10 2004+): оверлей перевода не читается заново OCR."""
    try:
        ok = bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))     # WDA_EXCLUDEFROMCAPTURE
        if not ok:
            log.info("SetWindowDisplayAffinity не сработал: код %s, Windows build %s",
                     ctypes.get_last_error(), getattr(sys.getwindowsversion(), "build", "?"))
        return ok
    except Exception:
        return False


def hide_path(path) -> None:
    """Делает папку скрытой в проводнике (для секретного хранилища)."""
    try:
        kernel32.SetFileAttributesW(str(path), 0x02)               # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass
