"""Умный менеджер буфера обмена + OCR & QR — точка входа.

Запуск без консоли:  pythonw main.py
"""
from __future__ import annotations

import logging
import logging.handlers
import shutil
import sys
import threading
from pathlib import Path

import settings as cfg


def setup_logging() -> None:
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(cfg.CONFIG_DIR / "app.log", maxBytes=500_000,
                                                   backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


setup_logging()
log = logging.getLogger("main")

import winutil  # noqa: E402  (после настройки логов)

if winutil.already_running():
    sys.exit(0)

from PyQt6.QtCore import Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox, QSystemTrayIcon  # noqa: E402

import theme  # noqa: E402
import translator  # noqa: E402
from clipboard_monitor import ClipboardMonitor  # noqa: E402
from database import Database  # noqa: E402
from ocr_engines import OcrHub  # noqa: E402
from ocr_translator import OcrResultWindow, ScreenGrabber, run_async  # noqa: E402
from overlay import OverlayController  # noqa: E402
from ui_main import HistoryPopup, SettingsDialog, TrayIcon  # noqa: E402

HK_HISTORY, HK_OCR, HK_OVERLAY, HK_SECRET, HK_NOTE = 1, 2, 3, 4, 5
CLEANUP_INTERVAL_MS = 6 * 60 * 60 * 1000


def _install_excepthooks() -> None:
    crash = logging.getLogger("crash")

    def hook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):          # Ctrl+C в консоли — просто закрываем приложение
            app = QApplication.instance()
            if app:
                app.quit()
            return
        crash.error("Необработанная ошибка", exc_info=(t, v, tb))

    # Без своего excepthook PyQt6 аварийно завершает процесс при любой ошибке в слоте.
    sys.excepthook = hook
    threading.excepthook = lambda a: crash.error("Ошибка в потоке", exc_info=(a.exc_type, a.exc_value,
                                                                             a.exc_traceback))


class Ctx:
    """Общий контекст: к нему обращаются все модули (db подменяется при смене профиля)."""
    settings = None
    db = None
    ocr = None
    monitor = None
    profile = "main"                 # main | secret

    @staticmethod
    def activity() -> None:          # вызывается при действиях пользователя (для автовыхода из секретного режима)
        pass


class App:
    def __init__(self) -> None:
        winutil.set_app_id()
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)
        self.qt.setApplicationName("Умный буфер обмена")
        self.qt.setStyleSheet(theme.QSS)
        _install_excepthooks()

        self.ctx = Ctx()
        self.ctx.settings = cfg.Settings()
        self._notes: list[str] = []
        self.main_db = self._open_main_db()
        self.secret_db: Database | None = None
        self.ctx.db = self.main_db
        self.ctx.ocr = OcrHub(self.ctx.settings)
        self.ctx.monitor = ClipboardMonitor(self.ctx)
        self.ctx.activity = self._activity

        self.popup = HistoryPopup(self.ctx)
        self.grabber = ScreenGrabber()
        self.ocr_window = OcrResultWindow(self.ctx)
        self.overlay = OverlayController(self.ctx)
        self.tray = TrayIcon()
        self.hotkeys = None
        self.secret_timer = QTimer()
        self.secret_timer.setSingleShot(True)
        self.secret_timer.timeout.connect(self._secret_timeout)

        self.ctx.monitor.changed.connect(self.popup.on_data_changed)
        self.ctx.monitor.changed.connect(self._activity)
        self.grabber.captured.connect(self.ocr_window.process)
        self.overlay.state_changed.connect(self.tray.set_overlay_active)
        self.tray.open_history.connect(self.popup.toggle)
        self.tray.scan_screen.connect(self.scan_screen)
        self.tray.new_note.connect(self.popup.new_note)
        self.tray.overlay_region.connect(self.overlay_region)
        self.tray.overlay_full.connect(self.overlay_full)
        self.tray.open_settings.connect(self.open_settings)
        self.tray.pause_toggled.connect(lambda paused: setattr(self.ctx.monitor, "paused", paused))
        self.tray.secret_toggled.connect(self._tray_secret)
        self.tray.quit_requested.connect(self.qt.quit)
        self.qt.aboutToQuit.connect(self.shutdown)

        self.cleanup_timer = QTimer()
        self.cleanup_timer.timeout.connect(self.run_cleanup)
        self.cleanup_timer.start(CLEANUP_INTERVAL_MS)
        QTimer.singleShot(15_000, self.run_cleanup)       # первая очистка вскоре после старта

        s = self.ctx.settings
        if s.get("autostart"):
            winutil.set_autostart(True)                    # обновить путь, если программу перенесли
        self.tray.set_secret_available(bool(s.get("secret_enabled")))
        self.apply_hotkeys()

    # ---------- база данных ----------
    def _open_main_db(self) -> Database:
        root = Path(self.ctx.settings.get("root"))
        try:
            return Database(root)
        except Exception:
            log.exception("Не удалось открыть папку хранения %s", root)
            fallback = Path.home() / cfg.APP_NAME
            self._notes.append(f"Папка «{root}» недоступна — история хранится в «{fallback}».")
            self.ctx.settings.set("root", str(fallback))
            return Database(fallback)

    def run_cleanup(self) -> None:
        days = int(self.ctx.settings.get("cleanup_days") or 0)
        if days <= 0:
            return
        for db in (self.main_db, self.secret_db):
            if db is not None:
                run_async(db.cleanup, lambda n, err: log.info("Автоочистка: удалено %s", n), days)

    # ---------- секретный режим ----------
    def _secret_root(self) -> Path:
        return Path(self.ctx.settings.get("root")) / ".secret"

    def _ask_pin(self, stored: str) -> bool:
        dlg = QInputDialog()
        dlg.setWindowTitle("Секретный режим")
        dlg.setLabelText("PIN:")
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        QTimer.singleShot(60, lambda: winutil.force_foreground(int(dlg.winId())))
        if not dlg.exec():
            return False
        if cfg.verify_pin(dlg.textValue(), stored):
            return True
        self.tray.showMessage("Секретный режим", "Неверный PIN.", QSystemTrayIcon.MessageIcon.Warning, 3000)
        return False

    def enter_secret(self) -> bool:
        s = self.ctx.settings
        if not s.get("secret_enabled") or self.ctx.profile == "secret":
            return False
        stored = s.get("secret_pin")
        if stored and not self._ask_pin(stored):
            return False
        try:
            if self.secret_db is None:
                root = self._secret_root()
                self.secret_db = Database(root)
                winutil.hide_path(root)
        except Exception:
            log.exception("Не удалось открыть секретное хранилище")
            self.tray.showMessage("Секретный режим", "Не удалось открыть хранилище.",
                                  QSystemTrayIcon.MessageIcon.Warning, 4000)
            return False
        self.popup.hide_popup()
        self.ctx.db = self.secret_db
        self.ctx.profile = "secret"
        self.popup.set_secret(True)
        self.tray.set_secret(True)
        self._activity()
        return True

    def leave_secret(self) -> None:
        if self.ctx.profile != "secret":
            return
        self.popup.hide_popup()
        self.ctx.db = self.main_db
        self.ctx.profile = "main"
        self.secret_timer.stop()
        self.popup.set_secret(False)
        self.tray.set_secret(False)
        translator.clear_cache()                            # не держим в памяти переводы секретных данных

    def toggle_secret(self) -> None:
        if self.ctx.profile == "secret":
            self.leave_secret()
        else:
            self.enter_secret()

    def _tray_secret(self, checked: bool) -> None:
        if checked:
            if not self.enter_secret():
                self.tray.set_secret(False)
        else:
            self.leave_secret()

    def _activity(self) -> None:
        """Любое действие в секретном режиме сбрасывает таймер автовыхода."""
        if self.ctx.profile != "secret":
            return
        minutes = int(self.ctx.settings.get("secret_timeout_min") or 0)
        if minutes > 0:
            self.secret_timer.start(minutes * 60_000)
        else:
            self.secret_timer.stop()

    def _secret_timeout(self) -> None:
        if self.ctx.profile == "secret":
            self.leave_secret()
            self.tray.showMessage("Умный буфер обмена", "Секретный режим выключен из-за бездействия.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)

    # ---------- хоткеи ----------
    def apply_hotkeys(self) -> None:
        s = self.ctx.settings
        keys = {HK_HISTORY: s.get("hotkey_history"), HK_OCR: s.get("hotkey_ocr")}
        if s.get("overlay_hotkey"):
            keys[HK_OVERLAY] = s.get("overlay_hotkey")
        if s.get("secret_enabled") and s.get("secret_hotkey"):
            keys[HK_SECRET] = s.get("secret_hotkey")
        if s.get("note_hotkey"):
            keys[HK_NOTE] = s.get("note_hotkey")
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.hotkeys = winutil.HotkeyThread(keys)
        self.hotkeys.pressed.connect(self._on_hotkey)
        self.hotkeys.failed.connect(lambda msg: self.tray.showMessage(
            "Горячая клавиша", msg, QSystemTrayIcon.MessageIcon.Warning, 6000))
        self.hotkeys.start()
        self.tray.set_hotkeys(s.get("hotkey_history"), s.get("hotkey_ocr"), s.get("overlay_hotkey") or "—")

    def _on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == HK_HISTORY:
            self.popup.toggle()
        elif hotkey_id == HK_OCR:
            self.scan_screen()
        elif hotkey_id == HK_OVERLAY:
            self.overlay_region()
        elif hotkey_id == HK_SECRET:
            self.toggle_secret()
        elif hotkey_id == HK_NOTE:
            self.popup.new_note()

    def scan_screen(self) -> None:
        self.popup.hide_popup()
        QTimer.singleShot(180, self.grabber.start)         # дать окну истории исчезнуть с экрана

    def overlay_region(self) -> None:
        self.popup.hide_popup()
        if self.overlay.active:
            self.overlay.stop()
        else:
            QTimer.singleShot(180, self.overlay.grabber.start)

    def overlay_full(self) -> None:
        self.popup.hide_popup()
        if self.overlay.active:
            self.overlay.stop()
        else:
            QTimer.singleShot(180, self.overlay.start_fullscreen)

    # ---------- настройки ----------
    def open_settings(self) -> None:
        self.popup.hide_popup()
        dlg = SettingsDialog(self.ctx)
        if dlg.exec():                                     # 1 = «Сохранить»
            self.apply_settings(dlg.values())

    def apply_settings(self, values: dict) -> None:
        s = self.ctx.settings
        old_root = s.get("root")
        new_root = values["root"]
        pin_new = values.pop("secret_pin_new", None)
        pin_clear = values.pop("secret_pin_clear", False)
        to_save = {k: v for k, v in values.items() if k != "root"}
        if pin_clear:
            to_save["secret_pin"] = ""
        elif pin_new:
            to_save["secret_pin"] = cfg.hash_pin(pin_new)
        s.update(to_save)
        winutil.set_autostart(bool(values["autostart"]))
        self.ctx.ocr.reset()
        translator.reset_cooldowns()
        if not s.get("secret_enabled") and self.ctx.profile == "secret":
            self.leave_secret()
        self.tray.set_secret_available(bool(s.get("secret_enabled")))
        self.overlay.reload_settings()
        self.apply_hotkeys()
        if Path(new_root) != Path(old_root):
            self.change_root(old_root, new_root)

    def change_root(self, old_root: str, new_root: str) -> None:
        answer = QMessageBox.question(
            None, "Папка хранения",
            "Перенести существующую историю и картинки в новую папку?\n"
            "(Старая папка останется нетронутой. Секретное хранилище переносится вместе с ней.)")
        try:
            Path(new_root).mkdir(parents=True, exist_ok=True)
            self.leave_secret()
            self.main_db.close()
            if self.secret_db is not None:
                self.secret_db.close()
                self.secret_db = None
            if answer == QMessageBox.StandardButton.Yes:
                self.qt.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    shutil.copytree(old_root, new_root, dirs_exist_ok=True)
                finally:
                    self.qt.restoreOverrideCursor()
            self.main_db = Database(new_root)
            self.ctx.db = self.main_db
            self.ctx.settings.set("root", new_root)
            self.popup.refresh()
        except Exception:
            log.exception("Смена папки хранения не удалась")
            QMessageBox.warning(None, "Папка хранения",
                                "Не удалось переключиться на новую папку. Продолжаю работать со старой.")
            self.main_db = Database(old_root)
            self.ctx.db = self.main_db

    # ---------- жизненный цикл ----------
    def shutdown(self) -> None:
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
            self.overlay.stop()
            for db in (self.main_db, self.secret_db):
                if db is not None:
                    db.close()
            import local_translate
            local_translate.ENGINE.unload()
        except Exception:
            log.exception("Ошибка при завершении")

    def run(self) -> int:
        self.tray.show()
        s = self.ctx.settings
        message = f"Работает в трее. {s.get('hotkey_history')} — история, {s.get('hotkey_ocr')} — сканер экрана."
        if self._notes:
            message += "\n" + "\n".join(self._notes)
        self.tray.showMessage("Умный буфер обмена", message, QSystemTrayIcon.MessageIcon.Information, 5000)
        QTimer.singleShot(1200, self.ctx.monitor.scan_now)        # что было скопировано до запуска (дубли отсекаются)
        if s.get("import_history"):
            self.ctx.monitor.import_history()                     # журнал буфера Windows (Win+V)
        QTimer.singleShot(3000, winutil.trim_memory)
        return self.qt.exec()


if __name__ == "__main__":
    sys.exit(App().run())
