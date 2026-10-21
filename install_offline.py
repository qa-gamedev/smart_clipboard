"""Необязательный запасной офлайн-перевод (Argos Translate). Основной офлайн-вариант — NLLB-200
(install_model.py): он сильнее и переводит напрямую; Argos нужен только как дополнительный запас.

    pip install argostranslate
    python install_offline.py ko ja de          # языки, ИЗ которых нужно переводить
    python install_offline.py --to ru ko ja     # целевой язык (по умолчанию ru)

Скрипт ничего не скрывает: показывает, какие модели есть в каталоге, что установлено и результат теста.
"""
import sys


def main() -> int:
    args = sys.argv[1:]
    target = "ru"
    if "--to" in args:
        i = args.index("--to")
        target = args[i + 1]
        del args[i:i + 2]
    sources = args or ["en"]

    try:
        import argostranslate.package as pkg
        import argostranslate.translate as at
    except ImportError:
        print("Сначала выполните:  pip install argostranslate")
        return 1

    print("Обновляю каталог моделей (нужен интернет)…")
    try:
        pkg.update_package_index()
        available = pkg.get_available_packages()
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось получить каталог: {e}\nПроверьте интернет/VPN и повторите.")
        return 1
    print(f"В каталоге моделей: {len(available)}")

    def find(a: str, b: str):
        return next((p for p in available if p.from_code == a and p.to_code == b), None)

    wanted = []
    for src in sources:
        if src == target:
            continue
        if find(src, target):
            wanted.append((src, target))
        else:                                                  # через английский
            if src != "en":
                wanted.append((src, "en"))
            if target != "en":
                wanted.append(("en", target))
    failed = False
    for a, b in dict.fromkeys(wanted):
        package = find(a, b)
        if package is None:
            print(f"  ✗ в каталоге нет модели {a}→{b}")
            failed = True
            continue
        try:
            print(f"  скачиваю {a}→{b}…")
            pkg.install_from_path(package.download())
            print(f"  ✓ установлено {a}→{b}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {a}→{b}: {e}")
            failed = True

    print("\nУстановленные языки:", ", ".join(sorted(l.code for l in at.get_installed_languages())) or "нет")
    langs = {l.code: l for l in at.get_installed_languages()}
    for src in sources:
        if src in langs and target in langs and src != target:
            tr = langs[src].get_translation(langs[target])
            print(f"  тест {src}→{target}:", "работает" if tr else "НЕТ пути перевода")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
