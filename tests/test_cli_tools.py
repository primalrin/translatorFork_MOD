import os
import zipfile
from argparse import Namespace

import pytest

from gemini_translator.cli import (
    CliError,
    TaskPlan,
    _choose_translation_rel_path,
    _collect_untranslated_fix_items,
    _load_translated_chapter_records,
    _resolve_api_keys,
    _resolve_model,
    _safe_settings_for_output,
    _scan_untranslated_records,
    build_parser,
    build_session_settings,
    build_task_plan,
    command_plan,
    command_build_epub,
    command_providers,
    command_translate,
    select_chapters,
)
from gemini_translator.utils.project_manager import TranslationProjectManager


def _build_epub(path):
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")
        epub.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        )
        epub.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
""",
        )
        epub.writestr("OEBPS/ch1.xhtml", "<html><body><p>One</p></body></html>")
        epub.writestr("OEBPS/ch2.xhtml", "<html><body><p>Two</p></body></html>")


class _FakeApiConfig:
    def __init__(self):
        self.providers = {
            "fake": {
                "models": {
                    "Known Model": {"id": "known-model-id", "rpm": 7},
                    "Fallback Model": {"id": "fallback-model-id", "rpm": 9},
                }
            }
        }

    def ensure_dynamic_provider_models(self, provider_id):
        self.dynamic_provider = provider_id

    def api_providers(self):
        return self.providers

    def default_model_name(self):
        return "Fallback Model"

    def provider_requires_api_key(self, provider_id):
        return True

    def provider_placeholder_api_key(self, provider_id):
        return f"placeholder:{provider_id}"

    def provider_max_instances(self, provider_id):
        return None

    def default_prompt(self):
        return "prompt"

    def all_models(self):
        return {
            model_name: {**model_config, "provider": provider_id}
            for provider_id, provider in self.providers.items()
            for model_name, model_config in provider.get("models", {}).items()
        }


class _NoKeysSettingsManager:
    def load_full_session_settings(self):
        return {}

    def get_custom_prompt(self):
        return ""

    def load_key_statuses(self):
        raise AssertionError("plan settings must not read configured API keys")


def _session_args(tmp_path, **overrides):
    values = {
        "provider": "fake",
        "model": "Known Model",
        "api_key": None,
        "api_key_file": None,
        "all_keys": False,
        "workers": None,
        "rpm": None,
        "temperature": None,
        "mode": "single",
        "task_size": None,
        "splits": 1,
        "force_accept": False,
        "json_epub": False,
        "prompt_file": None,
        "glossary": None,
        "settings_json": None,
        "project": str(tmp_path),
        "epub": str(tmp_path / "book.epub"),
    }
    values.update(overrides)
    return Namespace(**values)


def test_select_chapters_pending_skips_project_map_entries(tmp_path):
    epub_path = tmp_path / "book.epub"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _build_epub(epub_path)

    translated = project_dir / "OEBPS" / "ch1_translated.html"
    translated.parent.mkdir()
    translated.write_text("<html><body><p>One translated</p></body></html>", encoding="utf-8")

    manager = TranslationProjectManager(str(project_dir))
    manager.register_translation(
        "OEBPS/ch1.xhtml",
        "_translated.html",
        os.path.relpath(translated, project_dir).replace("\\", "/"),
    )

    assert select_chapters(str(epub_path), manager, mode="pending") == ["OEBPS/ch2.xhtml"]
    assert select_chapters(str(epub_path), manager, mode="translated") == ["OEBPS/ch1.xhtml"]


def test_resolve_model_accepts_explicit_model_id():
    api_config = _FakeApiConfig()

    model_name, model_config = _resolve_model(api_config, "fake", {}, "known-model-id")

    assert model_name == "Known Model"
    assert model_config["id"] == "known-model-id"
    assert model_config["provider"] == "fake"


def test_resolve_model_rejects_unknown_explicit_model_with_available_models():
    api_config = _FakeApiConfig()

    with pytest.raises(CliError) as exc_info:
        _resolve_model(api_config, "fake", {"model": "Known Model"}, "missing-model")

    assert "missing-model" in str(exc_info.value)
    assert exc_info.value.payload["provider"] == "fake"
    assert exc_info.value.payload["available_models"] == ["Known Model", "Fallback Model"]
    assert exc_info.value.payload["available_model_ids"] == ["known-model-id", "fallback-model-id"]


def test_resolve_model_falls_back_only_without_explicit_model():
    api_config = _FakeApiConfig()

    model_name, model_config = _resolve_model(api_config, "fake", {"model": "missing-model"}, None)

    assert model_name == "Fallback Model"
    assert model_config["id"] == "fallback-model-id"


class _KeyStatusSettingsManager:
    def __init__(self, statuses, limited_keys):
        self.statuses = statuses
        self.limited_keys = set(limited_keys)
        self.checked = []

    def load_key_statuses(self):
        return self.statuses

    def is_key_limit_active(self, key_info, model_id):
        self.checked.append((key_info["key"], model_id))
        return key_info["key"] in self.limited_keys


def test_resolve_api_keys_excludes_keys_with_an_active_model_limit(tmp_path):
    statuses = [
        {"key": "blocked-key", "provider": "fake", "status_by_model": {}},
        {"key": "available-key", "provider": "fake", "status_by_model": {}},
    ]
    manager = _KeyStatusSettingsManager(statuses, {"blocked-key"})

    keys = _resolve_api_keys(
        _FakeApiConfig(),
        manager,
        "fake",
        "known-model-id",
        {"active_keys_by_provider": {"fake": ["blocked-key", "available-key"]}},
        _session_args(tmp_path),
    )

    assert keys == ["available-key"]
    assert manager.checked == [
        ("blocked-key", "known-model-id"),
        ("available-key", "known-model-id"),
    ]


def test_resolve_api_keys_reports_when_every_selected_key_is_limited(tmp_path):
    secret = "limited-secret-key"
    statuses = [{"key": secret, "provider": "fake", "status_by_model": {}}]
    manager = _KeyStatusSettingsManager(statuses, {secret})

    with pytest.raises(CliError) as exc_info:
        _resolve_api_keys(
            _FakeApiConfig(),
            manager,
            "fake",
            "known-model-id",
            {"active_keys_by_provider": {"fake": [secret]}},
            _session_args(tmp_path),
        )

    assert "temporarily limited" in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert secret not in str(exc_info.value.payload)


def test_build_session_settings_can_skip_api_key_resolution(monkeypatch, tmp_path):
    from gemini_translator import cli

    monkeypatch.setattr(cli, "_ensure_api_config_initialized", lambda: _FakeApiConfig())
    args = _session_args(tmp_path)

    settings = build_session_settings(
        _NoKeysSettingsManager(),
        project_manager=None,
        chapters=[],
        args=args,
        require_api_keys=False,
    )

    assert settings["provider"] == "fake"
    assert settings["model"] == "Known Model"
    assert settings["api_keys"] == []
    assert settings["num_instances"] == 1


def test_build_task_plan_uses_batch_mode(tmp_path):
    epub_path = tmp_path / "book.epub"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _build_epub(epub_path)

    settings = {
        "file_path": str(epub_path),
        "output_folder": str(project_dir),
        "use_batching": True,
        "chunking": False,
        "task_size_limit": 10000,
    }
    chapters = ["OEBPS/ch1.xhtml", "OEBPS/ch2.xhtml"]

    plan = build_task_plan(str(epub_path), chapters, settings, TranslationProjectManager(str(project_dir)))

    assert plan.summary["task_count"] == 1
    assert plan.summary["task_types"] == {"epub_batch": 1}
    assert plan.payloads[0][2] == tuple(chapters)


def test_choose_translation_rel_path_prefers_explicit_suffix():
    versions = {
        "_translated.html": "a.html",
        "_validated.html": "b.html",
    }

    assert _choose_translation_rel_path(versions, "_translated.html") == "a.html"
    assert _choose_translation_rel_path(versions) == "b.html"


def test_build_epub_prefers_validated_translation_over_newer_retry(tmp_path):
    epub_path = tmp_path / "book.epub"
    project_dir = tmp_path / "project"
    output_path = tmp_path / "book_out.epub"
    project_dir.mkdir()
    _build_epub(epub_path)

    old_path = project_dir / "OEBPS" / "ch1_validated.html"
    retry_path = project_dir / "OEBPS" / "ch1_translated_retry.html"
    old_path.parent.mkdir()
    old_path.write_text("<html><body><p>old accepted</p></body></html>", encoding="utf-8")
    retry_path.write_text("<html><body><p>fresh retry</p></body></html>", encoding="utf-8")
    os.utime(old_path, (1000, 1000))
    os.utime(retry_path, (2000, 2000))

    manager = TranslationProjectManager(str(project_dir))
    manager.register_translation(
        "OEBPS/ch1.xhtml",
        "_validated.html",
        os.path.relpath(old_path, project_dir).replace("\\", "/"),
    )
    manager.register_translation(
        "OEBPS/ch1.xhtml",
        "_translated_retry.html",
        os.path.relpath(retry_path, project_dir).replace("\\", "/"),
    )

    payload = command_build_epub(
        Namespace(
            epub=str(epub_path),
            project=str(project_dir),
            output=str(output_path),
            provider=None,
            suffix=None,
            chapter=["OEBPS/ch1.xhtml"],
            offset=0,
            limit=None,
            strict=True,
        )
    )

    assert payload["replaced_count"] == 1
    with zipfile.ZipFile(output_path, "r") as epub:
        assert "OEBPS/ch1_validated.html" in epub.namelist()
        assert "old accepted" in epub.read("OEBPS/ch1_validated.html").decode("utf-8")


def test_safe_settings_masks_active_keys_by_provider():
    safe = _safe_settings_for_output({
        "api_keys": ["abcd1234"],
        "active_keys_by_provider": {
            "gemini": ["full-secret-key"],
            "local": [],
        },
        "custom_prompt": "prompt",
    })

    assert safe["api_keys"] == ["...1234"]
    assert safe["active_keys_by_provider"]["gemini"] == ["...-key"]
    assert "full-secret-key" not in str(safe)
    assert safe["custom_prompt_chars"] == 6


def test_new_cli_commands_parse_common_arguments():
    parser = build_parser()

    args = parser.parse_args(["providers"])
    assert args.func.__name__ == "command_providers"
    assert args.diagnose is False

    args = parser.parse_args(["providers", "--no-discovery"])
    assert args.func.__name__ == "command_providers"
    assert args.no_discovery is True

    args = parser.parse_args(["providers", "--doctor"])
    assert args.func.__name__ == "command_providers"
    assert args.diagnose is True

    args = parser.parse_args(["models", "--provider", "gemini"])
    assert args.func.__name__ == "command_models"
    assert args.provider == "gemini"

    args = parser.parse_args([
        "consistency",
        "--epub", "book.epub",
        "--project", "project",
        "--consistency-mode", "fast",
        "--suffix", "_validated.html",
    ])
    assert args.func.__name__ == "command_consistency"
    assert args.chapters == "translated"
    assert args.suffix == "_validated.html"

    args = parser.parse_args([
        "untranslated-fix",
        "--epub", "book.epub",
        "--project", "project",
        "--dry-run",
    ])
    assert args.func.__name__ == "command_untranslated_fix"
    assert args.dry_run is True


def test_command_providers_does_not_run_discovery_by_default(monkeypatch):
    from gemini_translator import cli

    class FakeRuntime:
        def bootstrap(self, *, include_engine):
            assert include_engine is False
            return Namespace(settings_manager=Namespace(load_key_statuses=lambda: []))

        def shutdown(self):
            pass

    class FakeApiConfig:
        def api_providers(self):
            return {
                "dynamic": {
                    "display_name": "Dynamic",
                    "visible": True,
                    "dynamic_model_discovery": True,
                    "models": {"Static": {"id": "static"}},
                }
            }

        def provider_requires_api_key(self, provider_id):
            return False

        def ensure_dynamic_provider_models(self, provider_id, force=False):
            raise AssertionError("providers must not discover models by default")

    monkeypatch.setattr(cli, "HeadlessRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_ensure_api_config_initialized", lambda: FakeApiConfig())

    payload = command_providers(Namespace(all=False, diagnose=False, no_discovery=False))

    assert payload["ok"] is True
    assert payload["diagnose"] is False
    assert payload["providers"][0]["id"] == "dynamic"
    assert payload["providers"][0]["dynamic_model_discovery"] is True
    assert payload["providers"][0]["discovery_checked"] is False
    assert "diagnostic" not in payload["providers"][0]

    payload = command_providers(Namespace(all=False, diagnose=True, no_discovery=True))

    assert payload["diagnose"] is False
    assert payload["providers"][0]["discovery_checked"] is False
    assert "diagnostic" not in payload["providers"][0]


def test_command_providers_diagnose_reports_dynamic_provider_availability(monkeypatch):
    from gemini_translator import cli

    class FakeRuntime:
        def bootstrap(self, *, include_engine):
            assert include_engine is False
            return Namespace(settings_manager=Namespace(load_key_statuses=lambda: []))

        def shutdown(self):
            pass

    class FakeApiConfig:
        def api_providers(self):
            return {
                "dynamic": {
                    "display_name": "Dynamic",
                    "visible": True,
                    "dynamic_model_discovery": True,
                    "models": {},
                },
                "static": {
                    "display_name": "Static",
                    "visible": True,
                    "models": {"Known": {"id": "known"}},
                },
            }

        def provider_requires_api_key(self, provider_id):
            return False

        def _provider_uses_dynamic_model_discovery(self, provider_id, provider=None):
            return provider_id == "dynamic"

        def _iter_local_discovery_sources(self, provider):
            return [{"label": "Local test", "root_url": "http://127.0.0.1:9999"}]

        def _discover_models_for_local_source(self, source, include_details=False):
            assert include_details is False
            return True, [{"id": "model-a"}, {"id": "model-b"}]

    monkeypatch.setattr(cli, "HeadlessRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_ensure_api_config_initialized", lambda: FakeApiConfig())

    payload = command_providers(Namespace(all=False, diagnose=True, no_discovery=False))
    providers = {provider["id"]: provider for provider in payload["providers"]}

    assert payload["ok"] is True
    assert payload["diagnose"] is True
    assert providers["dynamic"]["discovery_checked"] is True
    assert providers["dynamic"]["diagnostic"]["available"] is True
    assert providers["dynamic"]["diagnostic"]["available_source_count"] == 1
    assert providers["dynamic"]["diagnostic"]["discovered_model_count"] == 2
    assert providers["static"]["dynamic_model_discovery"] is False
    assert providers["static"]["discovery_checked"] is False


def test_command_plan_shuts_down_when_selection_fails(monkeypatch, tmp_path):
    from gemini_translator import cli

    runtimes = []

    class FakeRuntime:
        def __init__(self):
            self.shutdown_called = False
            runtimes.append(self)

        def bootstrap(self, *, include_engine):
            assert include_engine is False
            return Namespace(settings_manager=object())

        def shutdown(self):
            self.shutdown_called = True

    monkeypatch.setattr(cli, "HeadlessRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_project_manager", lambda project_folder: object())

    def raise_selection_error(*args, **kwargs):
        raise CliError("selection failed")

    monkeypatch.setattr(cli, "select_chapters", raise_selection_error)

    args = Namespace(
        project=str(tmp_path / "project"),
        epub=str(tmp_path / "book.epub"),
        chapters="pending",
        chapter=[],
        offset=0,
        limit=None,
    )

    with pytest.raises(CliError):
        command_plan(args)

    assert runtimes[0].shutdown_called is True


def test_command_translate_reports_failed_task_as_not_ok(monkeypatch, tmp_path):
    from gemini_translator import cli

    runtimes = []

    class FakeSignal:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    class FakeEventBus:
        def __init__(self):
            self.event_posted = FakeSignal()
            self.data = {}
            self.popped = []

        def set_data(self, key, value):
            self.data[key] = value

        def pop_data(self, key, default=None):
            self.popped.append(key)
            return self.data.pop(key, default)

    class FakeTaskManager:
        def __init__(self):
            self.pending_tasks = None
            self.pending_task_chains = None

        def set_pending_tasks(self, payloads):
            self.pending_tasks = payloads

        def set_pending_task_chains(self, task_chains):
            self.pending_task_chains = task_chains

    class FakeApp:
        def __init__(self):
            self.settings_manager = object()
            self.task_manager = FakeTaskManager()
            self.event_bus = FakeEventBus()
            self.exec_called = False

        def exec(self):
            self.exec_called = True

    class FakeQTimer:
        @staticmethod
        def singleShot(ms, callback):
            callback()

    class FakeRuntime:
        def __init__(self):
            self.shutdown_called = False
            self.app = FakeApp()
            self.app_main = Namespace(QtCore=Namespace(QTimer=FakeQTimer))
            runtimes.append(self)

        def bootstrap(self, *, include_engine):
            assert include_engine is True
            return self.app

        def shutdown(self):
            self.shutdown_called = True

    class FakeObserver:
        def __init__(self, app, *, verbose, timeout_sec):
            self.app = app
            self.verbose = verbose
            self.timeout_sec = timeout_sec

        def result_payload(self, task_manager):
            return {
                "finished": True,
                "timed_out": False,
                "task_events": {"total": 1, "success": 0, "failed": 1},
            }

    monkeypatch.setattr(cli, "HeadlessRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "CliSessionObserver", FakeObserver)
    monkeypatch.setattr(cli, "_project_manager", lambda project_folder: object())
    monkeypatch.setattr(cli, "select_chapters", lambda *args, **kwargs: ["OEBPS/ch1.xhtml"])
    monkeypatch.setattr(cli, "build_session_settings", lambda *args, **kwargs: {"provider": "fake"})
    monkeypatch.setattr(
        cli,
        "build_task_plan",
        lambda epub_path, chapters, settings, project_manager=None: TaskPlan(
            chapters=chapters,
            payloads=[("epub", epub_path, chapters[0])],
            task_chains=[],
            settings=settings,
            summary={"task_count": 1},
        ),
    )

    args = Namespace(
        project=str(tmp_path / "project"),
        epub=str(tmp_path / "book.epub"),
        chapters="pending",
        chapter=[],
        offset=0,
        limit=None,
        verbose=False,
        timeout=None,
    )

    payload = command_translate(args)

    assert payload["ok"] is False
    assert payload["status"] == "finished"
    assert runtimes[0].app.exec_called is True
    assert runtimes[0].app.task_manager.pending_tasks == [("epub", str(tmp_path / "book.epub"), "OEBPS/ch1.xhtml")]
    assert runtimes[0].app.event_bus.popped == ["cli_session_active"]
    assert runtimes[0].shutdown_called is True


def test_untranslated_scan_reads_project_translations(tmp_path):
    epub_path = tmp_path / "book.epub"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _build_epub(epub_path)

    translated = project_dir / "OEBPS" / "ch1_validated.html"
    translated.parent.mkdir()
    translated.write_text("<html><body><p>Перевод Alpha остался.</p></body></html>", encoding="utf-8")

    manager = TranslationProjectManager(str(project_dir))
    manager.register_translation(
        "OEBPS/ch1.xhtml",
        "_validated.html",
        os.path.relpath(translated, project_dir).replace("\\", "/"),
    )

    records, missing = _load_translated_chapter_records(
        str(epub_path),
        str(project_dir),
        manager,
        ["OEBPS/ch1.xhtml"],
        suffix="_validated.html",
    )
    issues = _scan_untranslated_records(records, word_exceptions=set())

    assert missing == []
    assert records[0]["file"] == str(translated)
    assert issues[0]["chapter"] == "OEBPS/ch1.xhtml"
    assert "Alpha" in issues[0]["untranslated_words"]


def test_untranslated_fix_collector_groups_html_context(tmp_path):
    epub_path = tmp_path / "book.epub"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _build_epub(epub_path)

    translated = project_dir / "OEBPS" / "ch1_validated.html"
    translated.parent.mkdir()
    translated.write_text("<html><body><p>Перевод Alpha остался.</p></body></html>", encoding="utf-8")

    manager = TranslationProjectManager(str(project_dir))
    manager.register_translation(
        "OEBPS/ch1.xhtml",
        "_validated.html",
        os.path.relpath(translated, project_dir).replace("\\", "/"),
    )
    records, _ = _load_translated_chapter_records(
        str(epub_path),
        str(project_dir),
        manager,
        ["OEBPS/ch1.xhtml"],
        suffix="_validated.html",
    )

    data_items, soup_cache, scan_issues = _collect_untranslated_fix_items(records, word_exceptions=set())

    assert scan_issues
    assert str(translated) in soup_cache
    assert data_items[0]["internal_html_path"] == "OEBPS/ch1.xhtml"
    assert "Alpha" in data_items[0]["context"]
