"""Импорт истории буфера обмена Windows (Win+V) при запуске программы.

Что скопировано ДО запуска, программа сама увидеть не может — но Windows хранит последние ~25 элементов
своей истории буфера. Если она включена (Параметры → Система → Буфер обмена → «Журнал буфера обмена»),
эти элементы подтягиваются в нашу историю (дубли отсекаются по хешу).
Нужен пакет winrt-Windows.ApplicationModel.DataTransfer; без него и при выключенном журнале — тихо пропускаем.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


async def _read() -> list[tuple]:
    from winrt.windows.applicationmodel.datatransfer import (Clipboard, ClipboardHistoryItemsResultStatus,
                                                             StandardDataFormats)
    from winrt.windows.storage.streams import DataReader
    result = await Clipboard.get_history_items_async()
    if result.status != ClipboardHistoryItemsResultStatus.SUCCESS:
        log.info("Журнал буфера Windows недоступен или выключен: %s", result.status)
        return []
    out = []
    for item in result.items:
        try:
            stamp = item.timestamp.timestamp()
        except Exception:  # noqa: BLE001
            stamp = time.time()
        view = item.content
        try:
            if view.contains(StandardDataFormats.text):
                text = await view.get_text_async()
                if text and text.strip():
                    out.append(("text", text, stamp))
                    continue
            if view.contains(StandardDataFormats.bitmap):
                stream = await (await view.get_bitmap_async()).open_read_async()
                size = int(stream.size)
                if 0 < size < 40_000_000:
                    reader = DataReader(stream)
                    await reader.load_async(size)
                    buf = bytearray(size)
                    reader.read_bytes(buf)
                    out.append(("image", bytes(buf), stamp))
        except Exception:  # noqa: BLE001
            log.debug("Элемент журнала пропущен", exc_info=True)
    out.sort(key=lambda t: t[2])
    return out


def read_history() -> list[tuple]:
    """[(kind, payload, timestamp)] от старых к новым. Вызывать в фоновом потоке."""
    try:
        import winutil
        winutil.init_com_mta()
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_read())
        finally:
            loop.close()
    except ImportError:
        return []
    except Exception:  # noqa: BLE001
        log.info("Не удалось прочитать журнал буфера Windows", exc_info=True)
        return []
