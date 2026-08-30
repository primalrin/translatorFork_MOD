"""Запускает PyInstaller с данными, найденными на текущей машине сборки."""

from __future__ import annotations

import argparse
import os
import shutil

from build_master import (
    APP_ICON_FILE,
    HIDDEN_IMPORTS_BLOCK,
    MAIN_PY_FILE,
    PROJECT_ROOT,
    get_additional_data_entries,
    normalize_data_entry,
)


def _existing_data_entries():
    for item in get_additional_data_entries():
        source, destination = normalize_data_entry(item)
        if not source.is_absolute():
            source = PROJECT_ROOT / source
        if not source.exists():
            print(f"     [WARN] Элемент не найден и будет пропущен: {source}")
            continue
        yield source, destination


def build_pyinstaller_args(*, mode: str, app_name: str, collect_data_modules: list[str]) -> list[str]:
    args = [
        str(PROJECT_ROOT / MAIN_PY_FILE),
        "--windowed",
        f"--name={app_name}",
        "--clean",
        f"--icon={PROJECT_ROOT / APP_ICON_FILE}",
        "--noconfirm",
    ]

    try:
        import pyinstaller_hooks_contrib

        hook_dirs = pyinstaller_hooks_contrib.get_hook_dirs()
    except (ImportError, IndexError):
        hook_dirs = []
    if hook_dirs:
        args.append(f"--additional-hooks-dir={hook_dirs[0]}")

    args.extend(f"--collect-data={module}" for module in sorted(set(collect_data_modules)))
    args.extend(f"--hidden-import={module}" for module in HIDDEN_IMPORTS_BLOCK)

    if mode in {"portable", "hybrid"}:
        args.append("--onefile")

    if mode == "portable":
        for source, destination in _existing_data_entries():
            args.append(f"--add-data={source}{os.pathsep}{destination}")

    return args


def copy_external_data(*, app_name: str, mode: str) -> None:
    if mode not in {"hybrid", "advanced"}:
        return

    output_root = PROJECT_ROOT / "dist"
    if mode == "advanced":
        output_root /= app_name

    print("[+] Копирование внешних данных...")
    for source, destination in _existing_data_entries():
        target_dir = output_root / destination
        if source.is_dir():
            shutil.copytree(source, target_dir, dirs_exist_ok=True)
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_dir / source.name)
    print("[OK] Данные скопированы.")


def run_build(*, mode: str, app_name: str, collect_data_modules: list[str]) -> None:
    from PyInstaller.__main__ import run as run_pyinstaller

    os.chdir(PROJECT_ROOT)
    run_pyinstaller(
        build_pyinstaller_args(
            mode=mode,
            app_name=app_name,
            collect_data_modules=collect_data_modules,
        )
    )
    copy_external_data(app_name=app_name, mode=mode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("portable", "hybrid", "advanced"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--collect-data", action="append", default=[])
    args = parser.parse_args()
    run_build(
        mode=args.mode,
        app_name=args.name,
        collect_data_modules=args.collect_data,
    )


if __name__ == "__main__":
    main()
