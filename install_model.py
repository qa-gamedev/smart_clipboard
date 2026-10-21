"""Скачивание офлайн-модели перевода NLLB-200 (≈630 МБ) из командной строки.

    pip install ctranslate2 sentencepiece
    python install_model.py

То же самое делает кнопка в Настройки → Распознавание и перевод. Загрузку можно прервать (Ctrl+C)
и запустить снова — файл докачается. Файлы проверяются по SHA-256.
"""
import sys

import local_translate as lt


def main() -> int:
    ok, deps = lt.deps_status()
    if not ok:
        print(f"Не хватает пакетов. Выполните:  {deps}\n")
    if lt.is_installed():
        print("Модель уже скачана:", lt.MODEL_DIR)
        return 0
    print("Скачиваю", lt.MODEL_REPO, "→", lt.MODEL_DIR)

    def progress(done, total, name):
        pct = f"{done * 100 / total:5.1f}%" if total else "  …  "
        print(f"\r{pct}  {done / 1e6:7.0f} МБ  {name:<40}", end="", flush=True)

    try:
        lt.download_model(progress=progress)
    except KeyboardInterrupt:
        print("\nПрервано. Запустите снова — загрузка продолжится.")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nОшибка: {e}")
        return 1
    print("\nГотово.", "" if ok else f"Осталось: {deps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
