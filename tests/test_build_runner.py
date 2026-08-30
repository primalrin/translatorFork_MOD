import importlib
import os
import sys
import types
from pathlib import Path

import build_master
import pytest


def test_playwright_discovery_honors_custom_browser_cache(tmp_path, monkeypatch):
    custom_cache = tmp_path / "browser cache with spaces"
    custom_cache.mkdir()

    monkeypatch.setattr(build_master, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_master.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(custom_cache))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    discovered = build_master.discover_playwright_runtime_data()

    assert (custom_cache, Path("playwright_runtime") / "ms-playwright") in discovered


def test_generated_launcher_resolves_optional_data_at_build_time(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "playwright_runtime"
    runtime_dir.mkdir()
    (runtime_dir / "node.exe").write_bytes(b"node")

    output_bat = tmp_path / "build.bat"
    monkeypatch.setattr(build_master, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_master, "OUTPUT_BAT_FILE", str(output_bat))

    build_master.generate_pure_bat_script([], set())

    launcher = output_bat.read_text(encoding="utf-8")
    assert '"%PYTHON_CMD%" build_runner.py --mode portable' in launcher
    assert '--add-data "playwright_runtime\\node.exe;playwright_runtime"' not in launcher


def test_portable_arguments_skip_data_that_is_missing_on_build_machine(tmp_path, monkeypatch):
    build_runner = importlib.import_module("build_runner")
    present = tmp_path / "README.md"
    present.write_text("ok", encoding="utf-8")
    missing = tmp_path / "playwright_runtime" / "node.exe"

    monkeypatch.setattr(
        build_runner,
        "get_additional_data_entries",
        lambda: [(present, Path(".")), (missing, Path("playwright_runtime"))],
    )

    args = build_runner.build_pyinstaller_args(mode="portable", app_name="translator work", collect_data_modules=[])

    joined = "\n".join(args)
    assert str(present) in joined
    assert str(missing) not in joined
    assert "--hidden-import=gemini_translator.api.handlers.gemini" in args


def test_portable_arguments_keep_paths_with_spaces_as_single_arguments(tmp_path, monkeypatch):
    build_runner = importlib.import_module("build_runner")
    runtime_root = tmp_path / "runtime with spaces"
    node = runtime_root / "node.exe"
    package = runtime_root / "package"
    browsers = tmp_path / "browser cache with spaces"
    runtime_destination = Path("playwright_runtime")
    package_destination = runtime_destination / "package"
    browsers_destination = runtime_destination / "ms-playwright"
    node.parent.mkdir()
    node.write_bytes(b"node")
    package.mkdir()
    browsers.mkdir()

    monkeypatch.setattr(build_runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        build_runner,
        "get_additional_data_entries",
        lambda: [
            (node, runtime_destination),
            (package, package_destination),
            (browsers, browsers_destination),
        ],
    )

    args = build_runner.build_pyinstaller_args(mode="portable", app_name="translator work", collect_data_modules=[])

    assert "--name=translator work" in args
    assert f"--add-data={node}{os.pathsep}{runtime_destination}" in args
    assert f"--add-data={package}{os.pathsep}{package_destination}" in args
    assert f"--add-data={browsers}{os.pathsep}{browsers_destination}" in args


@pytest.mark.parametrize(
    ("mode", "relative_output"),
    [
        ("hybrid", Path("dist")),
        ("advanced", Path("dist/translator work")),
    ],
)
def test_external_build_modes_copy_runtime_files_and_directories(tmp_path, monkeypatch, mode, relative_output):
    build_runner = importlib.import_module("build_runner")
    runtime_node = tmp_path / "source" / "node.exe"
    runtime_package = tmp_path / "source" / "package"
    runtime_node.parent.mkdir()
    runtime_node.write_bytes(b"node")
    runtime_package.mkdir()
    (runtime_package / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(build_runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        build_runner,
        "get_additional_data_entries",
        lambda: [
            (runtime_node, Path("playwright_runtime")),
            (runtime_package, Path("playwright_runtime/package")),
        ],
    )

    build_runner.copy_external_data(app_name="translator work", mode=mode)

    output_root = tmp_path / relative_output / "playwright_runtime"
    assert (output_root / "node.exe").read_bytes() == b"node"
    assert (output_root / "package" / "package.json").read_text(encoding="utf-8") == "{}"


def _install_fake_pyinstaller(monkeypatch, run):
    package = types.ModuleType("PyInstaller")
    package.__path__ = []
    main_module = types.ModuleType("PyInstaller.__main__")
    main_module.run = run
    monkeypatch.setitem(sys.modules, "PyInstaller", package)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", main_module)


def test_external_data_is_copied_only_after_pyinstaller_succeeds(monkeypatch):
    build_runner = importlib.import_module("build_runner")
    events = []
    _install_fake_pyinstaller(monkeypatch, lambda _args: events.append("pyinstaller"))
    monkeypatch.setattr(build_runner, "PROJECT_ROOT", Path.cwd())
    monkeypatch.setattr(build_runner, "build_pyinstaller_args", lambda **_kwargs: [])
    monkeypatch.setattr(
        build_runner,
        "copy_external_data",
        lambda **_kwargs: events.append("copy"),
    )

    build_runner.run_build(mode="advanced", app_name="translator work", collect_data_modules=[])

    assert events == ["pyinstaller", "copy"]


def test_external_data_is_not_copied_after_pyinstaller_failure(monkeypatch):
    build_runner = importlib.import_module("build_runner")
    events = []

    def fail(_args):
        events.append("pyinstaller")
        raise RuntimeError("build failed")

    _install_fake_pyinstaller(monkeypatch, fail)
    monkeypatch.setattr(build_runner, "PROJECT_ROOT", Path.cwd())
    monkeypatch.setattr(build_runner, "build_pyinstaller_args", lambda **_kwargs: [])
    monkeypatch.setattr(
        build_runner,
        "copy_external_data",
        lambda **_kwargs: events.append("copy"),
    )

    with pytest.raises(RuntimeError, match="build failed"):
        build_runner.run_build(mode="advanced", app_name="translator work", collect_data_modules=[])

    assert events == ["pyinstaller"]
