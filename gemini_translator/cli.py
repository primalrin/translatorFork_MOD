from __future__ import annotations

import argparse
import copy
import contextlib
import fnmatch
import json
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.auto_workflow_helpers import build_sequential_chapter_chains


class CliError(Exception):
    def __init__(self, message: str, *, exit_code: int = 2, payload: dict | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload or {}


@dataclass
class TaskPlan:
    chapters: list[str]
    payloads: list[tuple]
    task_chains: list[list[tuple]]
    settings: dict
    summary: dict


def _abs_path(path: str | os.PathLike | None) -> str:
    if not path:
        return ""
    return str(Path(path).expanduser().resolve())


def _load_json_file(path: str | os.PathLike | None, default: Any = None) -> Any:
    if not path:
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise CliError(f"JSON file not found: {path}")
    except json.JSONDecodeError as exc:
        raise CliError(f"Invalid JSON in {path}: {exc}")


def _load_text_file(path: str | os.PathLike | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        raise CliError(f"Text file not found: {path}")


def _write_json(payload: dict, *, pretty: bool = True) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


@contextlib.contextmanager
def _redirect_internal_stdout_to_stderr():
    """Keep stdout machine-readable while existing app code keeps using print()."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _ensure_api_config_initialized():
    from .api import config as api_config

    api_config.initialize_configs()
    return api_config


def get_epub_chapters(epub_path: str) -> list[str]:
    from .utils.epub_tools import get_epub_chapter_order

    epub_path = _abs_path(epub_path)
    if not os.path.exists(epub_path):
        raise CliError(f"EPUB file not found: {epub_path}")
    chapters = get_epub_chapter_order(epub_path)
    if not chapters:
        raise CliError(f"No translatable HTML/XHTML chapters found in: {epub_path}")
    return list(chapters)


def _matches_patterns(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    normalized = path.replace("\\", "/")
    basename = os.path.basename(normalized)
    for pattern in patterns:
        pattern = str(pattern or "").strip()
        if not pattern:
            continue
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
        if pattern.lower() in normalized.lower():
            return True
    return False


def select_chapters(
    epub_path: str,
    project_manager=None,
    *,
    mode: str = "pending",
    patterns: list[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[str]:
    mode = (mode or "pending").strip().lower()
    if mode not in {"all", "pending", "translated"}:
        raise CliError(f"Unsupported chapter mode: {mode}")

    chapters = get_epub_chapters(epub_path)
    patterns = patterns or []
    selected = [chapter for chapter in chapters if _matches_patterns(chapter, patterns)]

    if project_manager is not None and mode != "all":
        filtered = []
        for chapter in selected:
            has_translation = bool(project_manager.get_versions_for_original(chapter))
            if mode == "pending" and not has_translation:
                filtered.append(chapter)
            elif mode == "translated" and has_translation:
                filtered.append(chapter)
        selected = filtered

    offset = max(0, int(offset or 0))
    if offset:
        selected = selected[offset:]
    if limit is not None:
        limit = max(0, int(limit))
        selected = selected[:limit]
    return selected


def load_project_glossary(project_folder: str, glossary_path: str | None = None) -> dict[str, dict]:
    path = glossary_path or os.path.join(project_folder, "project_glossary.json")
    if not path or not os.path.exists(path):
        return {}
    data = _load_json_file(path, default=[])
    if isinstance(data, dict):
        result = {}
        for original, entry in data.items():
            if isinstance(entry, dict):
                result[str(original)] = {
                    "rus": str(entry.get("rus") or entry.get("translation") or ""),
                    "note": str(entry.get("note") or ""),
                }
            else:
                result[str(original)] = {"rus": str(entry or ""), "note": ""}
        return result
    if isinstance(data, list):
        result = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            original = str(entry.get("original") or "").strip()
            if not original:
                continue
            result[original] = {
                "rus": str(entry.get("rus") or entry.get("translation") or ""),
                "note": str(entry.get("note") or ""),
            }
        return result
    raise CliError(f"Unsupported glossary format: {path}")


def _chapter_sizes(
    epub_path: str,
    chapters: list[str],
    project_manager=None,
    task_size_unit: str | None = None,
) -> dict[str, int]:
    from .utils.epub_tools import (
        TASK_SIZE_UNIT_CHARS,
        estimate_epub_chapter_input_size,
        get_epub_chapter_sizes_with_cache,
        normalize_task_size_unit,
    )

    task_size_unit = normalize_task_size_unit(task_size_unit)
    sizes = {}
    if project_manager is not None and task_size_unit != TASK_SIZE_UNIT_CHARS:
        sizes.update(get_epub_chapter_sizes_with_cache(project_manager, epub_path) or {})

    missing = [chapter for chapter in chapters if int(sizes.get(chapter, 0) or 0) <= 0]
    if missing:
        with zipfile.ZipFile(epub_path, "r") as archive:
            for chapter in missing:
                try:
                    content = archive.read(chapter).decode("utf-8", "ignore")
                    sizes[chapter] = estimate_epub_chapter_input_size(content, task_size_unit)
                except Exception:
                    sizes[chapter] = 0
    return {chapter: int(sizes.get(chapter, 0) or 0) for chapter in chapters}


def _payload_chapters(payload: tuple) -> list[str]:
    task_type = payload[0] if payload else ""
    if task_type in {"epub", "epub_chunk"}:
        return [str(payload[2])]
    if task_type in {"epub_batch", "glossary_batch_task"}:
        return [str(item) for item in payload[2]]
    return []


def summarize_payloads(payloads: list[tuple]) -> dict:
    type_counts = Counter(payload[0] for payload in payloads if payload)
    unique_chapters = []
    seen = set()
    for payload in payloads:
        for chapter in _payload_chapters(payload):
            if chapter not in seen:
                seen.add(chapter)
                unique_chapters.append(chapter)
    return {
        "task_count": len(payloads),
        "chapter_count": len(unique_chapters),
        "task_types": dict(sorted(type_counts.items())),
        "chapters": unique_chapters,
    }


def _apply_mode_overrides(settings: dict, mode: str | None, args) -> None:
    mode = (mode or "saved").strip().lower()
    if mode == "saved":
        return
    if mode == "single":
        settings.update({
            "use_batching": False,
            "chunking": False,
            "sequential_translation": False,
        })
    elif mode == "batch":
        settings.update({
            "use_batching": True,
            "chunking": False,
            "sequential_translation": False,
        })
    elif mode == "chunk":
        settings.update({
            "use_batching": False,
            "chunking": True,
            "sequential_translation": False,
        })
    elif mode == "sequential":
        settings.update({
            "use_batching": False,
            "chunking": False,
            "sequential_translation": True,
            "sequential_translation_splits": int(getattr(args, "splits", 1) or 1),
        })
    else:
        raise CliError(f"Unsupported task mode: {mode}")


def _provider_models(api_config, provider_id: str) -> dict:
    api_config.ensure_dynamic_provider_models(provider_id)
    provider = api_config.api_providers().get(provider_id, {})
    models = provider.get("models", {})
    return models if isinstance(models, dict) else {}


def _normalized_model_config(model_config: Any, provider_id: str, model_name: str) -> dict:
    if isinstance(model_config, dict):
        cfg = dict(model_config)
    else:
        cfg = {"id": str(model_config or model_name).strip() or model_name}
    cfg.setdefault("provider", provider_id)
    return cfg


def _available_model_names(models_for_provider: dict) -> list[str]:
    return [str(model_name) for model_name in models_for_provider]


def _available_model_ids(models_for_provider: dict, provider_id: str) -> list[str]:
    seen = set()
    model_ids = []
    for model_name, model_config in models_for_provider.items():
        cfg = _normalized_model_config(model_config, provider_id, str(model_name))
        model_id = str(cfg.get("id") or "").strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            model_ids.append(model_id)
    return model_ids


def _find_provider_model(models_for_provider: dict, provider_id: str, model_ref: str | None) -> tuple[str, dict] | None:
    model_ref = str(model_ref or "").strip()
    if not model_ref:
        return None
    for model_name, model_config in models_for_provider.items():
        model_name = str(model_name)
        cfg = _normalized_model_config(model_config, provider_id, model_name)
        if model_name == model_ref or str(cfg.get("id") or "").strip() == model_ref:
            return model_name, cfg
    return None


def _resolve_provider(api_config, saved_settings: dict, provider_arg: str | None) -> str:
    providers = api_config.api_providers()
    provider_id = str(provider_arg or saved_settings.get("provider") or "").strip()
    if provider_id and provider_id in providers:
        return provider_id
    if provider_id:
        for candidate_id, provider in providers.items():
            if str(provider.get("display_name") or "").strip() == provider_id:
                return candidate_id
        raise CliError(f"Unknown provider: {provider_id}", payload={"available_providers": sorted(providers)})
    if "gemini" in providers:
        return "gemini"
    visible = [pid for pid, cfg in providers.items() if cfg.get("visible", True)]
    return visible[0] if visible else next(iter(providers))


def _resolve_model(api_config, provider_id: str, saved_settings: dict, model_arg: str | None) -> tuple[str, dict]:
    models_for_provider = _provider_models(api_config, provider_id)
    explicit = str(model_arg or "").strip()
    saved = str(saved_settings.get("model") or "").strip()

    if explicit:
        resolved = _find_provider_model(models_for_provider, provider_id, explicit)
        if resolved:
            return resolved
        raise CliError(
            f"Unknown model for provider {provider_id}: {explicit}",
            payload={
                "provider": provider_id,
                "available_models": _available_model_names(models_for_provider),
                "available_model_ids": _available_model_ids(models_for_provider, provider_id),
            },
        )

    for model_name in [saved, api_config.default_model_name()]:
        if not model_name:
            continue
        resolved = _find_provider_model(models_for_provider, provider_id, model_name)
        if resolved:
            return resolved

    if models_for_provider:
        model_name, model_config = next(iter(models_for_provider.items()))
        cfg = _normalized_model_config(model_config, provider_id, str(model_name))
        return model_name, cfg

    raise CliError(
        f"Provider has no models: {provider_id}",
        payload={"provider": provider_id, "available_models": [], "available_model_ids": []},
    )


def _read_api_key_file(path: str | None) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        raise CliError(f"API key file not found: {path}")


def _configured_provider_keys(key_statuses: list[dict], provider_id: str) -> list[str]:
    keys = [
        item.get("key")
        for item in key_statuses
        if isinstance(item, dict) and item.get("provider") == provider_id and item.get("key")
    ]
    return list(dict.fromkeys(keys))


def _resolve_api_keys(
    api_config,
    settings_manager,
    provider_id: str,
    model_id: str,
    saved_settings: dict,
    args,
) -> list[str]:
    if not api_config.provider_requires_api_key(provider_id):
        placeholder = api_config.provider_placeholder_api_key(provider_id)
        return [placeholder] if placeholder else []

    key_statuses = settings_manager.load_key_statuses()
    provider_statuses = {
        str(item.get("key")): item
        for item in key_statuses
        if isinstance(item, dict)
        and item.get("provider") == provider_id
        and item.get("key")
    }
    keys = []
    keys.extend(getattr(args, "api_key", None) or [])
    keys.extend(_read_api_key_file(getattr(args, "api_key_file", None)))
    keys = [str(key).strip() for key in keys if str(key).strip()]
    if not keys:
        active_by_provider = saved_settings.get("active_keys_by_provider")
        if isinstance(active_by_provider, dict):
            active = active_by_provider.get(provider_id)
            if isinstance(active, (list, tuple, set)):
                keys = [str(key).strip() for key in active if str(key).strip()]

    if getattr(args, "all_keys", False) or not keys:
        keys = _configured_provider_keys(key_statuses, provider_id)

    if not keys:
        raise CliError(
            f"No API keys available for provider: {provider_id}",
            payload={"hint": "Pass --api-key/--api-key-file or configure keys in the app."},
        )
    unique_keys = list(dict.fromkeys(keys))
    available_keys = [
        key
        for key in unique_keys
        if key not in provider_statuses
        or not settings_manager.is_key_limit_active(provider_statuses[key], model_id)
    ]
    if not available_keys:
        raise CliError(
            f"All API keys are temporarily limited for provider: {provider_id}",
            payload={
                "provider": provider_id,
                "model_id": model_id,
                "hint": "Wait for the provider quota reset or configure another key.",
            },
        )
    return available_keys


def build_session_settings(settings_manager, project_manager, chapters: list[str], args, *, require_api_keys: bool = True) -> dict:
    api_config = _ensure_api_config_initialized()
    saved_settings = settings_manager.load_full_session_settings() or {}
    if not isinstance(saved_settings, dict):
        saved_settings = {}

    provider_id = _resolve_provider(api_config, saved_settings, getattr(args, "provider", None))
    model_name, model_config = _resolve_model(api_config, provider_id, saved_settings, getattr(args, "model", None))
    api_keys = (
        _resolve_api_keys(
            api_config,
            settings_manager,
            provider_id,
            str(model_config.get("id") or ""),
            saved_settings,
            args,
        )
        if require_api_keys
        else []
    )

    prompt_text = _load_text_file(getattr(args, "prompt_file", None))
    if prompt_text is None:
        prompt_text = saved_settings.get("custom_prompt") or settings_manager.get_custom_prompt() or api_config.default_prompt()

    project_folder = _abs_path(getattr(args, "project", None))
    epub_path = _abs_path(getattr(args, "epub", None))
    glossary = load_project_glossary(project_folder, getattr(args, "glossary", None))

    settings = dict(saved_settings)
    settings.update({
        "provider": provider_id,
        "model": model_name,
        "model_config": model_config,
        "file_path": epub_path,
        "output_folder": project_folder,
        "api_keys": api_keys,
        "full_glossary_data": glossary,
        "custom_prompt": prompt_text,
        "auto_translation": {"enabled": False},
        "auto_start": True,
        "project_manager": project_manager,
        "use_batching": bool(saved_settings.get("use_batching", False)),
        "chunking": bool(saved_settings.get("chunking", False)),
        "chunk_on_error": bool(saved_settings.get("chunk_on_error", False)),
        "sequential_translation": bool(saved_settings.get("sequential_translation", False)),
        "sequential_translation_splits": int(saved_settings.get("sequential_translation_splits", 1) or 1),
        "task_size_limit": int(saved_settings.get("task_size_limit", 30000) or 30000),
        "dynamic_glossary": bool(saved_settings.get("dynamic_glossary", True)),
        "use_jieba": bool(saved_settings.get("use_jieba", False)),
        "segment_cjk_text": bool(saved_settings.get("segment_cjk_text", False)),
        "fuzzy_threshold": int(saved_settings.get("fuzzy_threshold", 100) or 100),
        "rpm_limit": int(getattr(args, "rpm", None) or saved_settings.get("rpm_limit", model_config.get("rpm", 10)) or 10),
        "rpd_limit": int(saved_settings.get("rpd_limit", 0) or 0),
        "temperature": float(getattr(args, "temperature", None) if getattr(args, "temperature", None) is not None else saved_settings.get("temperature", 1.0)),
        "temperature_override_enabled": bool(getattr(args, "temperature", None) is not None or saved_settings.get("temperature_override_enabled", False)),
        "use_system_instruction": bool(saved_settings.get("use_system_instruction", False)),
        "system_instruction": saved_settings.get("system_instruction"),
        "thinking_enabled": bool(saved_settings.get("thinking_enabled", False)),
        "thinking_budget": saved_settings.get("thinking_budget"),
        "thinking_level": saved_settings.get("thinking_level"),
        "force_accept": bool(getattr(args, "force_accept", False) or saved_settings.get("force_accept", False)),
        "use_json_epub_pipeline": bool(getattr(args, "json_epub", False) or saved_settings.get("use_json_epub_pipeline", False)),
        "use_prettify": bool(saved_settings.get("use_prettify", False)),
        "max_concurrent_requests": saved_settings.get("max_concurrent_requests"),
        "model_id": model_config.get("id"),
    })

    _apply_mode_overrides(settings, getattr(args, "mode", None), args)
    if getattr(args, "task_size", None):
        settings["task_size_limit"] = int(args.task_size)
        settings["task_size_limit_user_defined"] = True

    provider_limit = api_config.provider_max_instances(provider_id)
    requested_workers = getattr(args, "workers", None) or saved_settings.get("num_instances") or len(api_keys)
    try:
        requested_workers = int(requested_workers)
    except (TypeError, ValueError):
        requested_workers = 1
    max_workers = len(api_keys)
    if provider_limit:
        max_workers = min(max_workers, provider_limit)
    settings["num_instances"] = max(1, min(max_workers, requested_workers))

    if settings.get("sequential_translation"):
        settings["sequential_chapter_order"] = get_epub_chapters(epub_path)
        chapter_chains = build_sequential_chapter_chains(
            chapters,
            settings.get("sequential_translation_splits", 1),
        )
        if chapter_chains:
            settings["sequential_translation_splits"] = len(chapter_chains)
        settings["sequential_chain_starts"] = [chain[0] for chain in chapter_chains if chain]

    settings_override = _load_json_file(getattr(args, "settings_json", None), default=None)
    if isinstance(settings_override, dict):
        settings.update(settings_override)
        if "model_config" not in settings_override:
            settings["model_config"] = api_config.all_models().get(settings.get("model"), settings["model_config"])
        settings["model_id"] = (settings.get("model_config") or {}).get("id")

    return settings


def build_task_plan(epub_path: str, chapters: list[str], settings: dict, project_manager=None) -> TaskPlan:
    from .utils.glossary_tools import TaskPreparer

    sizes = _chapter_sizes(epub_path, chapters, project_manager, settings.get("task_size_unit"))
    preparer = TaskPreparer(settings, sizes)
    if settings.get("sequential_translation"):
        chapter_chains = build_sequential_chapter_chains(
            chapters,
            settings.get("sequential_translation_splits", 1),
        )
        task_chains = [preparer.prepare_tasks(chain) for chain in chapter_chains if chain]
        payloads = [payload for chain in task_chains for payload in chain]
    else:
        task_chains = []
        payloads = preparer.prepare_tasks(chapters)

    summary = summarize_payloads(payloads)
    summary.update({
        "sequential": bool(settings.get("sequential_translation")),
        "chain_count": len(task_chains),
        "total_source_tokens": sum(sizes.values()),
        "total_source_metric": "gemini_input_tokens",
        "total_source_chars": sum(sizes.values()),
    })
    return TaskPlan(chapters=chapters, payloads=payloads, task_chains=task_chains, settings=settings, summary=summary)


def _safe_settings_for_output(settings: dict) -> dict:
    hidden = dict(settings)
    if hidden.get("api_keys"):
        hidden["api_keys"] = [f"...{str(key)[-4:]}" for key in hidden.get("api_keys", [])]
    active_by_provider = hidden.get("active_keys_by_provider")
    if isinstance(active_by_provider, dict):
        hidden["active_keys_by_provider"] = {
            str(provider): [f"...{str(key)[-4:]}" for key in (keys or [])]
            for provider, keys in active_by_provider.items()
            if isinstance(keys, (list, tuple, set))
        }
    hidden.pop("project_manager", None)
    if hidden.get("custom_prompt"):
        hidden["custom_prompt_chars"] = len(str(hidden.pop("custom_prompt")))
    if hidden.get("full_glossary_data"):
        hidden["glossary_terms"] = len(hidden.pop("full_glossary_data"))
    return hidden


def _queue_status_counts(task_manager) -> dict[str, int]:
    counts = {"pending": 0, "in_progress": 0, "failed": 0, "completed": 0, "held": 0}
    try:
        with task_manager._light_read_conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
    except Exception:
        pass
    return counts


class CliSessionObserver:
    def __init__(
        self,
        app,
        *,
        verbose: bool = False,
        timeout_sec: int | None = None,
        capture_results: bool = False,
    ):
        from PyQt6 import QtCore

        self.app = app
        self.QtCore = QtCore
        self.verbose = verbose
        self.timeout_sec = timeout_sec
        self.started_at = time.time()
        self.session_id = None
        self.finished = False
        self.timed_out = False
        self.reason = None
        self.task_events = []
        self.task_results = []
        self.event_counts = Counter()
        self.logs = []
        self.capture_results = bool(capture_results)
        app.event_bus.event_posted.connect(self.on_event)
        if timeout_sec:
            QtCore.QTimer.singleShot(int(timeout_sec * 1000), self.on_timeout)

    def on_event(self, event: dict):
        event_name = event.get("event")
        self.event_counts[event_name] += 1
        data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}

        if event_name == "log_message":
            message = str(data.get("message") or "")
            if message:
                self.logs.append(message)
                if self.verbose:
                    print(message, file=sys.stderr)
            return

        if event_name == "session_started":
            self.session_id = data.get("session_id") or event.get("session_id")
            return

        if event_name == "task_finished":
            task_info = data.get("task_info")
            task_payload = None
            if isinstance(task_info, (tuple, list)) and len(task_info) >= 2:
                task_payload = task_info[1]
            if self.capture_results:
                self.task_results.append({
                    "success": bool(data.get("success")),
                    "task_info": task_info,
                    "task_type": task_payload[0] if task_payload else None,
                    "result_data": data.get("result_data"),
                    "message": data.get("message"),
                    "error_type": data.get("error_type"),
                })
            self.task_events.append({
                "success": bool(data.get("success")),
                "error_type": data.get("error_type"),
                "message": data.get("message"),
                "task_type": task_payload[0] if task_payload else None,
                "chapters": _payload_chapters(tuple(task_payload)) if task_payload else [],
            })
            return

        if event_name == "session_finished":
            self.finished = True
            self.reason = data.get("reason") or "session_finished"
            self.QtCore.QTimer.singleShot(750, self.app.quit)

    def on_timeout(self):
        if self.finished:
            return
        self.timed_out = True
        self.reason = f"timeout after {self.timeout_sec}s"
        try:
            self.app.event_bus.event_posted.emit({
                "event": "manual_stop_requested",
                "source": "cli",
                "data": {"reason": self.reason},
            })
        finally:
            self.QtCore.QTimer.singleShot(5000, self.app.quit)

    def result_payload(self, task_manager) -> dict:
        counts = _queue_status_counts(task_manager)
        return {
            "finished": self.finished,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "session_id": self.session_id,
            "elapsed_sec": round(time.time() - self.started_at, 3),
            "queue": counts,
            "task_events": {
                "total": len(self.task_events),
                "success": sum(1 for event in self.task_events if event.get("success")),
                "failed": sum(1 for event in self.task_events if not event.get("success")),
            },
            "event_counts": dict(sorted(self.event_counts.items())),
            "recent_logs": self.logs[-20:],
        }


class HeadlessRuntime:
    def __init__(self):
        self.app = None
        self.app_main = None

    def bootstrap(self, *, include_engine: bool):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import main as app_main

        self.app_main = app_main
        app_main.prepare_console_streams()
        app_main.os_patch.PatientLock.register_vip_thread(threading.get_ident())

        app = app_main.ApplicationWithContext(["translator-cli"])
        self.app = app

        if sys.platform == "win32":
            app_main.asyncio.set_event_loop_policy(app_main.asyncio.WindowsSelectorEventLoopPolicy())

        app_main.os_patch.apply()
        app_main.api_config.initialize_configs()
        app.event_bus = app_main.EventBus()
        app.initialize_managers()
        app.settings_manager = app.get_settings_manager()
        app.global_version = getattr(app_main, "APP_VERSION", "")

        if include_engine:
            app_main.initialize_global_resources(app)
            app.task_manager = app_main.ChapterQueueManager(event_bus=app.event_bus)
            app.proxy_controller = app_main.GlobalProxyController(app.event_bus)
            proxy_settings = app.settings_manager.load_proxy_settings()
            app.proxy_controller.apply_settings(proxy_settings)
            temp_folder = os.path.join(os.path.expanduser("~"), ".epub_translator_temp")
            os.makedirs(temp_folder, exist_ok=True)
            app.context_manager = app_main.ContextManager(temp_folder)
            app.server_manager = app_main.ServerManager(app.event_bus)
            app.engine = app_main.TranslationEngine(task_manager=app.task_manager)
            app.engine_thread = app_main.QtCore.QThread(app)
            app.engine.moveToThread(app.engine_thread)
            app.engine_thread.finished.connect(app.engine.deleteLater)
            app.engine_thread.start()
            app_main.QtCore.QMetaObject.invokeMethod(
                app.engine,
                "log_thread_identity",
                app_main.QtCore.Qt.ConnectionType.QueuedConnection,
            )

        return app

    def shutdown(self):
        if not self.app:
            return
        app = self.app
        app_main = self.app_main
        try:
            if hasattr(app, "settings_manager") and app.settings_manager:
                app.settings_manager.flush()
        except Exception:
            pass
        if hasattr(app, "proxy_controller"):
            app.proxy_controller.shutdown()
        if app_main and hasattr(app, "engine_thread") and app.engine_thread.isRunning():
            try:
                app_main.QtCore.QMetaObject.invokeMethod(
                    app.engine,
                    "cleanup",
                    app_main.QtCore.Qt.ConnectionType.BlockingQueuedConnection,
                )
            except Exception:
                pass
            app.engine_thread.quit()
            app.engine_thread.wait(10000)


def _project_manager(project_folder: str):
    from .utils.project_manager import TranslationProjectManager

    project_folder = _abs_path(project_folder)
    os.makedirs(project_folder, exist_ok=True)
    return TranslationProjectManager(project_folder)


def _run_task_session(
    app,
    runtime: HeadlessRuntime,
    settings: dict,
    payloads: list[tuple],
    *,
    verbose: bool = False,
    timeout: int | None = None,
    capture_results: bool = False,
) -> tuple[dict, list[dict]]:
    app.task_manager.clear_all_queues()
    app.task_manager.set_pending_tasks(payloads)
    observer = CliSessionObserver(
        app,
        verbose=verbose,
        timeout_sec=timeout,
        capture_results=capture_results,
    )

    def start_session():
        app.event_bus.event_posted.emit({
            "event": "start_session_requested",
            "source": "cli",
            "data": {"settings": settings},
        })

    app.event_bus.set_data("cli_session_active", True)
    runtime.app_main.QtCore.QTimer.singleShot(0, start_session)
    app.exec()
    app.event_bus.pop_data("cli_session_active", None)
    return observer.result_payload(app.task_manager), list(observer.task_results)


def _session_completed_ok(result: dict) -> bool:
    task_events = result.get("task_events") if isinstance(result, dict) else {}
    failed = task_events.get("failed", 0) if isinstance(task_events, dict) else 0
    return bool(result.get("finished") and not result.get("timed_out") and int(failed or 0) == 0)


def _settings_with_single_task_mode(args):
    if getattr(args, "mode", "saved") == "saved":
        setattr(args, "mode", "single")
    return args


def _glossary_dict_to_list(glossary: dict[str, dict]) -> list[dict]:
    result = []
    for original, entry in (glossary or {}).items():
        if not str(original).strip():
            continue
        data = entry if isinstance(entry, dict) else {}
        result.append({
            "original": str(original),
            "rus": str(data.get("rus") or data.get("translation") or ""),
            "note": str(data.get("note") or ""),
        })
    return result


def _read_epub_member(epub_path: str, internal_path: str) -> str:
    with zipfile.ZipFile(epub_path, "r") as archive:
        return archive.read(internal_path).decode("utf-8", "ignore")


def command_status(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    api_config = _ensure_api_config_initialized()
    settings = app.settings_manager.load_full_session_settings() or {}
    providers = {}
    key_statuses = app.settings_manager.load_key_statuses()
    keys_by_provider = Counter(
        item.get("provider")
        for item in key_statuses
        if isinstance(item, dict) and item.get("provider")
    )
    for provider_id, provider in api_config.api_providers().items():
        models = provider.get("models", {})
        providers[provider_id] = {
            "display_name": provider.get("display_name") or provider_id,
            "visible": bool(provider.get("visible", True)),
            "requires_api_key": api_config.provider_requires_api_key(provider_id),
            "configured_keys": int(keys_by_provider.get(provider_id, 0)),
            "models": list(models.keys()) if isinstance(models, dict) else [],
            "file_suffix": provider.get("file_suffix"),
        }

    payload = {
        "ok": True,
        "settings_file": app.settings_manager.config_file,
        "settings_dir": app.settings_manager.config_dir,
        "settings_scope": getattr(app.settings_manager, "settings_scope", "default"),
        "settings_profile": getattr(app.settings_manager, "settings_profile", ""),
        "saved_provider": settings.get("provider"),
        "saved_model": settings.get("model"),
        "providers": providers,
        "project_history": app.settings_manager.load_project_history(),
    }

    if getattr(args, "project", None):
        pm = _project_manager(args.project)
        project_map = pm.get_full_map()
        payload["project"] = {
            "path": _abs_path(args.project),
            "mapped_chapters": len(project_map),
            "glossary_terms": len(load_project_glossary(_abs_path(args.project))),
        }
    if getattr(args, "epub", None):
        payload["epub"] = {
            "path": _abs_path(args.epub),
            "chapters": len(get_epub_chapters(args.epub)),
        }
    runtime.shutdown()
    return payload


def _provider_uses_dynamic_model_discovery(api_config, provider_id: str, provider: dict) -> bool:
    detector = getattr(api_config, "_provider_uses_dynamic_model_discovery", None)
    if callable(detector):
        try:
            return bool(detector(provider_id, provider))
        except TypeError:
            return bool(detector(provider_id))
    return provider_id == "local" or bool((provider or {}).get("dynamic_model_discovery"))


def _diagnose_dynamic_provider(api_config, provider_id: str, provider: dict) -> dict:
    sources_factory = getattr(api_config, "_iter_local_discovery_sources", None)
    source_discoverer = getattr(api_config, "_discover_models_for_local_source", None)

    if callable(sources_factory) and callable(source_discoverer):
        sources = []
        available_source_count = 0
        discovered_model_count = 0
        try:
            discovery_sources = list(sources_factory(provider or {}))
        except Exception as exc:
            return {
                "ok": False,
                "available": False,
                "source_count": 0,
                "available_source_count": 0,
                "discovered_model_count": 0,
                "sources": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

        for source in discovery_sources:
            source_payload = {
                "label": source.get("label"),
                "root_url": source.get("root_url"),
                "available": False,
                "model_count": 0,
            }
            try:
                is_successful, model_entries = source_discoverer(source, include_details=False)
                source_payload["available"] = bool(is_successful)
                if is_successful:
                    model_count = len(model_entries or [])
                    source_payload["model_count"] = model_count
                    available_source_count += 1
                    discovered_model_count += model_count
            except Exception as exc:
                source_payload["error"] = f"{type(exc).__name__}: {exc}"
            sources.append(source_payload)

        return {
            "ok": True,
            "available": available_source_count > 0,
            "source_count": len(sources),
            "available_source_count": available_source_count,
            "discovered_model_count": discovered_model_count,
            "sources": sources,
        }

    try:
        try:
            refreshed = api_config.ensure_dynamic_provider_models(provider_id, force=True)
        except TypeError:
            refreshed = api_config.ensure_dynamic_provider_models(provider_id)
        models = (refreshed or {}).get("models", {}) if isinstance(refreshed, dict) else {}
        return {
            "ok": True,
            "available": bool(models),
            "source_count": None,
            "available_source_count": None,
            "discovered_model_count": len(models) if isinstance(models, dict) else 0,
            "sources": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "available": False,
            "source_count": None,
            "available_source_count": None,
            "discovered_model_count": 0,
            "sources": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def command_providers(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    api_config = _ensure_api_config_initialized()
    diagnose = bool(getattr(args, "diagnose", False) and not getattr(args, "no_discovery", False))
    key_statuses = app.settings_manager.load_key_statuses()
    keys_by_provider = Counter(
        item.get("provider")
        for item in key_statuses
        if isinstance(item, dict) and item.get("provider")
    )
    providers = []
    for provider_id, provider in api_config.api_providers().items():
        if not getattr(args, "all", False) and not provider.get("visible", True):
            continue
        uses_dynamic_discovery = _provider_uses_dynamic_model_discovery(api_config, provider_id, provider)
        models = provider.get("models", {})
        provider_payload = {
            "id": provider_id,
            "display_name": provider.get("display_name") or provider_id,
            "visible": bool(provider.get("visible", True)),
            "requires_api_key": api_config.provider_requires_api_key(provider_id),
            "configured_keys": int(keys_by_provider.get(provider_id, 0)),
            "model_count": len(models) if isinstance(models, dict) else 0,
            "file_suffix": provider.get("file_suffix"),
            "browser_based": bool(provider.get("browser_based") or provider.get("use_browser")),
            "dynamic_model_discovery": uses_dynamic_discovery,
            "discovery_checked": False,
        }
        if diagnose and uses_dynamic_discovery:
            provider_payload["discovery_checked"] = True
            provider_payload["diagnostic"] = _diagnose_dynamic_provider(api_config, provider_id, provider)
        providers.append(provider_payload)
    runtime.shutdown()
    return {"ok": True, "providers": providers, "diagnose": diagnose}


def command_models(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    api_config = _ensure_api_config_initialized()
    saved_settings = app.settings_manager.load_full_session_settings() or {}
    provider_id = _resolve_provider(api_config, saved_settings, getattr(args, "provider", None))
    models = _provider_models(api_config, provider_id)
    payload_models = []
    for name, config in models.items():
        config = config or {}
        payload_models.append({
            "name": name,
            "id": config.get("id"),
            "provider": config.get("provider") or provider_id,
            "rpm": config.get("rpm"),
            "rpd": config.get("rpd"),
            "max_output_tokens": config.get("max_output_tokens"),
            "context_window": config.get("context_window"),
            "supports_thinking": config.get("supports_thinking"),
        })
    runtime.shutdown()
    return {
        "ok": True,
        "provider": provider_id,
        "saved_model": saved_settings.get("model"),
        "models": payload_models,
    }


def command_settings(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    settings = app.settings_manager.load_full_session_settings() or {}
    payload = {
        "ok": True,
        "settings_file": app.settings_manager.config_file,
        "settings_dir": app.settings_manager.config_dir,
        "settings_scope": getattr(app.settings_manager, "settings_scope", "default"),
        "settings_profile": getattr(app.settings_manager, "settings_profile", ""),
        "settings": _safe_settings_for_output(settings),
    }
    runtime.shutdown()
    return payload


def command_plan(args) -> dict:
    runtime = HeadlessRuntime()
    try:
        app = runtime.bootstrap(include_engine=False)
        project_folder = _abs_path(args.project)
        epub_path = _abs_path(args.epub)
        pm = _project_manager(project_folder)
        chapters = select_chapters(
            epub_path,
            pm,
            mode=args.chapters,
            patterns=args.chapter or [],
            offset=args.offset,
            limit=args.limit,
        )
        settings = build_session_settings(app.settings_manager, pm, chapters, args, require_api_keys=False)
        plan = build_task_plan(epub_path, chapters, settings, pm)
        return {
            "ok": True,
            "epub": epub_path,
            "project": project_folder,
            "plan": plan.summary,
            "settings": _safe_settings_for_output(settings),
        }
    finally:
        runtime.shutdown()


def command_translate(args) -> dict:
    runtime = HeadlessRuntime()
    try:
        app = runtime.bootstrap(include_engine=True)
        project_folder = _abs_path(args.project)
        epub_path = _abs_path(args.epub)
        pm = _project_manager(project_folder)
        chapters = select_chapters(
            epub_path,
            pm,
            mode=args.chapters,
            patterns=args.chapter or [],
            offset=args.offset,
            limit=args.limit,
        )
        settings = build_session_settings(app.settings_manager, pm, chapters, args)
        plan = build_task_plan(epub_path, chapters, settings, pm)

        if not plan.payloads:
            return {
                "ok": True,
                "status": "no_tasks",
                "epub": epub_path,
                "project": project_folder,
                "plan": plan.summary,
            }

        if plan.task_chains:
            app.task_manager.set_pending_task_chains(plan.task_chains)
        else:
            app.task_manager.set_pending_tasks(plan.payloads)

        observer = CliSessionObserver(
            app,
            verbose=bool(args.verbose),
            timeout_sec=args.timeout,
        )

        def start_session():
            app.event_bus.event_posted.emit({
                "event": "start_session_requested",
                "source": "cli",
                "data": {"settings": settings},
            })

        app.event_bus.set_data("cli_session_active", True)
        try:
            runtime.app_main.QtCore.QTimer.singleShot(0, start_session)
            app.exec()
        finally:
            app.event_bus.pop_data("cli_session_active", None)

        result = observer.result_payload(app.task_manager)
        return {
            "ok": _session_completed_ok(result),
            "status": "finished" if result["finished"] else "stopped",
            "epub": epub_path,
            "project": project_folder,
            "plan": plan.summary,
            "result": result,
        }
    finally:
        runtime.shutdown()


def _choose_translation_rel_path(versions: dict, suffix: str | None = None) -> str | None:
    if not isinstance(versions, dict) or not versions:
        return None
    if suffix is not None:
        return versions.get(suffix)
    for preferred_suffix in ("_validated.html", ""):
        if versions.get(preferred_suffix):
            return versions[preferred_suffix]
    for version_suffix, rel_path in versions.items():
        if version_suffix != "filtered" and rel_path:
            return rel_path
    return next(iter(versions.values()), None)


def _load_translated_chapter_records(
    epub_path: str,
    project_folder: str,
    project_manager,
    chapters: list[str],
    *,
    suffix: str | None = None,
) -> tuple[list[dict], list[str]]:
    records = []
    missing = []
    for chapter in chapters:
        versions = project_manager.get_versions_for_original(chapter)
        rel_path = _choose_translation_rel_path(versions, suffix)
        if not rel_path:
            missing.append(chapter)
            continue

        full_path = os.path.join(project_folder, rel_path.replace("/", os.sep))
        if not os.path.exists(full_path):
            missing.append(chapter)
            continue

        with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
            translated_html = handle.read()
        try:
            source_html = _read_epub_member(epub_path, chapter)
        except Exception:
            source_html = ""

        records.append({
            "chapter": chapter,
            "name": os.path.basename(chapter),
            "rel_path": rel_path,
            "file": full_path,
            "translated_html": translated_html,
            "source_html": source_html,
        })
    return records, missing


def _build_consistency_chapters(records: list[dict], *, include_source: bool = True) -> list[dict]:
    chapters = []
    for record in records:
        payload = {
            "name": record["chapter"],
            "path": record["file"],
            "content": record.get("translated_html") or "",
        }
        if include_source:
            payload["source_path"] = record["chapter"]
            payload["source_content"] = record.get("source_html") or ""
        chapters.append(payload)
    return chapters


def _word_exceptions_from_project(settings_manager, project_folder: str, exceptions_path: str | None = None) -> set[str]:
    api_config = _ensure_api_config_initialized()
    if exceptions_path:
        exceptions_text = _load_text_file(exceptions_path) or ""
    else:
        exceptions_text = ""
        try:
            exceptions_text = settings_manager.get_last_word_exceptions_text()
        except Exception:
            exceptions_text = ""
        if not exceptions_text.strip():
            exceptions_text = api_config.default_word_exceptions()

    exceptions = {
        line.strip().lower()
        for line in str(exceptions_text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    cyrillic_re = re.compile(r"[\u0400-\u04FF]+")
    pure_word_re = re.compile(r"[\W\d_]+", re.UNICODE)
    for entry in load_project_glossary(project_folder).values():
        if not isinstance(entry, dict):
            continue
        for value in (entry.get("rus"), entry.get("translation"), entry.get("note")):
            if not value:
                continue
            residue = pure_word_re.sub(" ", cyrillic_re.sub(" ", str(value)))
            for word in residue.split():
                if len(word) >= 2:
                    exceptions.add(word.lower())
    return exceptions


def _scan_untranslated_records(
    records: list[dict],
    *,
    word_exceptions: set[str] | None = None,
    include_mixed_script: bool = True,
) -> list[dict]:
    from .ui.dialogs.validation_dialogs.untranslated_detector import UntranslatedWordDetector

    detector = UntranslatedWordDetector(word_exceptions or set())
    issues = []
    for record in records:
        html = record.get("translated_html") or ""
        words = detector.detect(html)
        mixed = detector.detect_mixed_script(html) if include_mixed_script else []
        if words or mixed:
            issues.append({
                "chapter": record["chapter"],
                "file": record["file"],
                "untranslated_words": words,
                "mixed_script": mixed,
                "problem_count": len(words) + len(mixed),
            })
    return issues


def _collect_untranslated_fix_items(
    records: list[dict],
    *,
    word_exceptions: set[str] | None = None,
    max_context_chars: int = 2000,
) -> tuple[list[dict], dict, list[dict]]:
    from bs4 import BeautifulSoup, Comment, Declaration, ProcessingInstruction
    from .ui.dialogs.validation_dialogs.untranslated_detector import UntranslatedWordDetector

    detector = UntranslatedWordDetector(word_exceptions or set())
    inline_tags = {
        "span", "a", "strong", "em", "b", "i", "u", "font",
        "small", "big", "sub", "sup", "strike", "code", "var", "cite",
    }
    safe_blocks = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd",
        "blockquote", "pre", "caption", "figcaption", "td", "th", "label",
    }
    dangerous_roots = {"body", "html", "main", "[document]"}

    grouped: dict[tuple[str, str], dict] = {}
    soup_cache = {}
    scan_issues = []

    for record_index, record in enumerate(records):
        html = record.get("translated_html") or ""
        words = detector.detect(html)
        mixed = detector.detect_mixed_script(html)
        if words or mixed:
            scan_issues.append({
                "chapter": record["chapter"],
                "file": record["file"],
                "untranslated_words": words,
                "mixed_script": mixed,
                "problem_count": len(words) + len(mixed),
            })
        if not words:
            continue

        soup = BeautifulSoup(html, "html.parser")
        soup_cache[record["file"]] = {"soup": soup, "record": record}
        processed_containers = set()

        for term in words:
            term_pattern = re.compile(re.escape(term), re.IGNORECASE)
            for node in soup.find_all(string=term_pattern):
                if not node.parent:
                    continue
                if isinstance(node, (ProcessingInstruction, Comment, Declaration)):
                    continue
                if node.find_parent(["head", "script", "style", "title"]):
                    continue

                effective_container = node.parent
                while effective_container and effective_container.name in inline_tags:
                    if effective_container.parent:
                        effective_container = effective_container.parent
                    else:
                        break

                container_name = getattr(effective_container, "name", None)
                use_orphan_mode = False
                if container_name in dangerous_roots:
                    use_orphan_mode = True
                elif container_name in safe_blocks:
                    use_orphan_mode = False
                else:
                    block_children = safe_blocks.union({"div", "section", "article", "table", "ul", "ol"})
                    use_orphan_mode = any(getattr(child, "name", None) in block_children for child in effective_container.children)

                target_object = node if use_orphan_mode else effective_container
                context_text = (
                    str(node).strip()
                    if use_orphan_mode
                    else "".join(str(child) for child in effective_container.contents).strip()
                )
                if not context_text:
                    continue
                if max_context_chars > 0 and len(context_text) > max_context_chars:
                    target_object = node
                    context_text = str(node).strip()
                    use_orphan_mode = True
                    if len(context_text) > max_context_chars:
                        context_text = context_text[:max(0, max_context_chars - 3)].rstrip() + "..."

                unique_id = id(target_object)
                if unique_id in processed_containers:
                    continue
                processed_containers.add(unique_id)

                key = (record["file"], context_text)
                if key not in grouped:
                    grouped[key] = {
                        "term": term,
                        "context": context_text,
                        "location_info": f"{record['chapter']} #{record_index + 1}",
                        "source_type": "system",
                        "internal_html_path": record["chapter"],
                        "file": record["file"],
                        "occurrences": [],
                    }
                grouped[key]["occurrences"].append({
                    "target": target_object,
                    "is_orphan": use_orphan_mode,
                    "file": record["file"],
                })

    return list(grouped.values()), soup_cache, scan_issues


def _build_untranslated_fix_payloads(data_items: list[dict], *, batch_size: int = 50) -> list[str]:
    batch_size = max(1, int(batch_size or 50))
    payloads = []
    for start in range(0, len(data_items), batch_size):
        html_parts = []
        for idx, data_item in enumerate(data_items[start:start + batch_size], start=start):
            text = data_item.get("new_context", data_item.get("context", ""))
            html_parts.append(f'<p data-id="{idx}">{text}</p>')
        payloads.append("<html><body>" + "\n".join(html_parts) + "</body></html>")
    return payloads


def _untranslated_fix_prompt(settings_manager, prompt_path: str | None = None) -> str:
    api_config = _ensure_api_config_initialized()
    prompt_text = _load_text_file(prompt_path)
    if prompt_text is None:
        try:
            prompt_text = settings_manager.get_last_untranslated_prompt_text()
        except Exception:
            prompt_text = ""
    if not str(prompt_text or "").strip():
        prompt_text = api_config.default_untranslated_prompt()
    try:
        from .ui.dialogs.validation_dialogs.untranslated_fixer_dialog import build_effective_untranslated_prompt

        return build_effective_untranslated_prompt(prompt_text)
    except Exception:
        return str(prompt_text or "").strip()


def _parse_untranslated_fix_changes(results: list[dict], data_items: list[dict]) -> tuple[list[dict], int]:
    from bs4 import BeautifulSoup

    changes = []
    translated_groups = 0
    for result in results:
        if not result.get("success"):
            continue
        html = result.get("result_data") or ""
        if not html:
            continue
        soup = BeautifulSoup(str(html), "html.parser")
        for paragraph in soup.find_all("p", attrs={"data-id": True}):
            try:
                idx = int(paragraph["data-id"])
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(data_items):
                continue
            translated_groups += 1
            new_context = paragraph.decode_contents()
            if new_context == data_items[idx].get("context"):
                continue
            updated = dict(data_items[idx])
            updated["new_context"] = new_context
            changes.append(updated)
    return changes, translated_groups


def _apply_untranslated_fix_changes(changes: list[dict], soup_cache: dict, *, dry_run: bool = False) -> dict:
    from bs4 import BeautifulSoup

    affected_files = set()
    replacements = 0
    for change in changes:
        new_text = change.get("new_context") or ""
        temp_soup = BeautifulSoup(new_text, "html.parser")
        content_to_insert = temp_soup.body if temp_soup.body else temp_soup

        for occurrence in change.get("occurrences", []):
            target = occurrence.get("target")
            file_path = occurrence.get("file")
            if target is None or not file_path:
                continue
            nodes_to_inject = [copy.copy(node) for node in content_to_insert.contents]
            if occurrence.get("is_orphan"):
                try:
                    target.replace_with(*nodes_to_inject)
                except TypeError:
                    if nodes_to_inject:
                        first = nodes_to_inject[0]
                        target.replace_with(first)
                        current = first
                        for extra_node in nodes_to_inject[1:]:
                            current.insert_after(extra_node)
                            current = extra_node
            else:
                target.clear()
                for node in nodes_to_inject:
                    target.append(node)
            affected_files.add(file_path)
            replacements += 1

    saved_count = 0
    if not dry_run:
        for file_path in sorted(affected_files):
            cached = soup_cache.get(file_path)
            if not cached:
                continue
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(str(cached["soup"]))
            saved_count += 1

    return {
        "groups_changed": len(changes),
        "replacements": replacements,
        "affected_files": sorted(affected_files),
        "saved_count": saved_count,
        "dry_run": bool(dry_run),
    }


def _resolve_build_suffix(args) -> str | None:
    if getattr(args, "suffix", None):
        return args.suffix
    if getattr(args, "provider", None):
        api_config = _ensure_api_config_initialized()
        provider = api_config.api_providers().get(args.provider)
        if not provider:
            raise CliError(f"Unknown provider: {args.provider}")
        return provider.get("file_suffix")
    return None


def command_build_epub(args) -> dict:
    from .utils.epub_tools import EpubUpdater
    from .utils.translation_versions import select_epub_build_translation_version

    project_folder = _abs_path(args.project)
    epub_path = _abs_path(args.epub)
    output_path = _abs_path(args.output) if args.output else str(Path(epub_path).with_name(f"{Path(epub_path).stem}_translated.epub"))
    pm = _project_manager(project_folder)
    suffix = _resolve_build_suffix(args)
    chapters = select_chapters(
        epub_path,
        pm,
        mode="all",
        patterns=args.chapter or [],
        offset=args.offset,
        limit=args.limit,
    )

    updater = EpubUpdater(epub_path)
    selected = []
    missing = []
    for chapter in chapters:
        versions = pm.get_versions_for_original(chapter)
        if suffix is None:
            rel_path = select_epub_build_translation_version(versions, project_folder)
        else:
            rel_path = _choose_translation_rel_path(versions, suffix)
        if not rel_path:
            missing.append(chapter)
            continue
        full_path = os.path.join(project_folder, rel_path.replace("/", os.sep))
        if not os.path.exists(full_path):
            missing.append(chapter)
            continue
        updater.add_replacement(chapter, full_path)
        selected.append({"chapter": chapter, "file": full_path})

    if args.strict and missing:
        raise CliError(
            "Not all chapters have selected translated files.",
            payload={"missing": missing[:50], "missing_count": len(missing)},
        )
    if not selected:
        raise CliError("No translated chapters found for EPUB build.")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    updater.update_and_save(output_path)
    return {
        "ok": True,
        "epub": epub_path,
        "project": project_folder,
        "output": output_path,
        "suffix": suffix,
        "replaced_count": len(selected),
        "missing_count": len(missing),
        "missing": missing[:50],
    }


def command_generate(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=True)
    _settings_with_single_task_mode(args)

    prompt_text = getattr(args, "prompt", None)
    if prompt_text is None:
        prompt_text = _load_text_file(getattr(args, "prompt_file", None))

    input_text = getattr(args, "text", None)
    if input_text is None:
        input_text = _load_text_file(getattr(args, "input", None))
    if input_text is None and not sys.stdin.isatty():
        input_text = sys.stdin.read()
    input_text = input_text if input_text is not None else ""

    settings = build_session_settings(app.settings_manager, None, [], args)
    payload = ("raw_text_translation", input_text, prompt_text, getattr(args, "label", None) or "CLI generation")
    result, task_results = _run_task_session(
        app,
        runtime,
        settings,
        [payload],
        verbose=bool(args.verbose),
        timeout=args.timeout,
        capture_results=True,
    )
    successful = [item for item in task_results if item.get("success")]
    runtime.shutdown()
    return {
        "ok": bool(_session_completed_ok(result) and successful),
        "status": "finished" if result["finished"] else "stopped",
        "provider": settings.get("provider"),
        "model": settings.get("model"),
        "text": successful[0].get("result_data") if successful else "",
        "result": result,
    }


def command_glossary_generate(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=True)
    _settings_with_single_task_mode(args)

    project_folder = _abs_path(args.project)
    epub_path = _abs_path(args.epub)
    pm = _project_manager(project_folder)
    chapters = select_chapters(
        epub_path,
        pm,
        mode=args.chapters,
        patterns=args.chapter or [],
        offset=args.offset,
        limit=args.limit,
    )
    if not chapters:
        runtime.shutdown()
        return {"ok": True, "status": "no_chapters", "epub": epub_path, "project": project_folder}

    settings = build_session_settings(app.settings_manager, pm, chapters, args)
    glossary_prompt = _load_text_file(getattr(args, "glossary_prompt_file", None))
    if glossary_prompt is not None:
        settings["glossary_generation_prompt"] = glossary_prompt
    else:
        settings.setdefault("glossary_generation_prompt", _ensure_api_config_initialized().default_glossary_prompt())
    settings["glossary_merge_mode"] = args.merge_mode
    settings["initial_glossary_list"] = _glossary_dict_to_list(load_project_glossary(project_folder, getattr(args, "glossary", None)))
    if getattr(args, "new_terms_limit", None) is not None:
        settings["new_terms_limit"] = int(args.new_terms_limit)

    batch_size = max(1, int(args.batch_size or 1))
    payloads = [
        ("glossary_batch_task", epub_path, tuple(chapters[index:index + batch_size]))
        for index in range(0, len(chapters), batch_size)
    ]
    result, _ = _run_task_session(
        app,
        runtime,
        settings,
        payloads,
        verbose=bool(args.verbose),
        timeout=args.timeout,
    )

    glossary_rows = 0
    unique_terms = 0
    try:
        with app.task_manager._light_read_conn() as conn:
            glossary_rows = int(conn.execute("SELECT COUNT(*) FROM glossary_results").fetchone()[0] or 0)
            unique_terms = int(conn.execute("SELECT COUNT(DISTINCT LOWER(TRIM(original))) FROM glossary_results").fetchone()[0] or 0)
    except Exception:
        pass

    runtime.shutdown()
    return {
        "ok": _session_completed_ok(result),
        "status": "finished" if result["finished"] else "stopped",
        "epub": epub_path,
        "project": project_folder,
        "chapters": chapters,
        "task_count": len(payloads),
        "merge_mode": args.merge_mode,
        "glossary_results": {
            "rows": glossary_rows,
            "unique_terms": unique_terms,
        },
        "result": result,
    }


def command_consistency(args) -> dict:
    from .core.consistency_engine import (
        DEEP_CONSISTENCY_MODE,
        FAST_PROOFREAD_MODE,
        ConsistencyEngine,
        normalize_consistency_mode,
    )

    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    _settings_with_single_task_mode(args)

    project_folder = _abs_path(args.project)
    epub_path = _abs_path(args.epub)
    pm = _project_manager(project_folder)
    suffix = getattr(args, "suffix", None)
    chapters = select_chapters(
        epub_path,
        pm,
        mode=args.chapters,
        patterns=args.chapter or [],
        offset=args.offset,
        limit=args.limit,
    )
    records, missing = _load_translated_chapter_records(epub_path, project_folder, pm, chapters, suffix=suffix)
    if not records:
        runtime.shutdown()
        raise CliError("No translated chapters found for consistency check.", payload={"missing": missing[:50]})

    settings = build_session_settings(app.settings_manager, pm, chapters, args)
    settings.update({
        "chunk_size": int(args.chunk_size or 3),
        "full_glossary_data": load_project_glossary(project_folder, getattr(args, "glossary", None)),
        "consistency_mode": normalize_consistency_mode(args.consistency_mode),
    })
    if getattr(args, "confidences", None):
        settings["consistency_fix_confidences"] = args.confidences

    consistency_chapters = _build_consistency_chapters(records, include_source=not args.no_source)
    engine = ConsistencyEngine(app.settings_manager)
    logs = []
    errors = []
    engine.log_message.connect(lambda message: logs.append(str(message)))
    engine.error_occurred.connect(lambda message: errors.append(str(message)))

    mode = FAST_PROOFREAD_MODE if settings["consistency_mode"] == FAST_PROOFREAD_MODE else DEEP_CONSISTENCY_MODE
    if getattr(args, "glossary_first", False) and mode != FAST_PROOFREAD_MODE:
        mode = "glossary_first"

    engine.analyze_chapters(consistency_chapters, settings, list(settings.get("api_keys") or []), mode=mode)
    problems = list(getattr(engine, "all_problems", []) or [])

    fix_payload = None
    if getattr(args, "fix", False) and problems:
        fixed = engine.fix_all_chapters(consistency_chapters, settings, list(settings.get("api_keys") or []))
        written = []
        if getattr(args, "write", False):
            for file_path, content in fixed.items():
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                written.append(file_path)
        fix_payload = {
            "changed_files": sorted(fixed.keys()),
            "written_files": sorted(written),
            "write": bool(args.write),
        }

    runtime.shutdown()
    return {
        "ok": not errors,
        "epub": epub_path,
        "project": project_folder,
        "mode": settings["consistency_mode"],
        "checked_chapters": [record["chapter"] for record in records],
        "missing_translations": missing[:50],
        "problem_count": len(problems),
        "problems": problems,
        "glossary_summary": engine.get_glossary_summary(),
        "fix": fix_payload,
        "errors": errors,
        "logs": logs[-50:],
    }


def command_untranslated_scan(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=False)
    project_folder = _abs_path(args.project)
    epub_path = _abs_path(args.epub)
    pm = _project_manager(project_folder)
    suffix = getattr(args, "suffix", None)
    chapters = select_chapters(
        epub_path,
        pm,
        mode=args.chapters,
        patterns=args.chapter or [],
        offset=args.offset,
        limit=args.limit,
    )
    records, missing = _load_translated_chapter_records(epub_path, project_folder, pm, chapters, suffix=suffix)
    exceptions = _word_exceptions_from_project(app.settings_manager, project_folder, getattr(args, "exceptions", None))
    issues = _scan_untranslated_records(
        records,
        word_exceptions=exceptions,
        include_mixed_script=not args.no_mixed_script,
    )
    runtime.shutdown()
    return {
        "ok": True,
        "epub": epub_path,
        "project": project_folder,
        "checked_chapters": len(records),
        "missing_translations": missing[:50],
        "problem_chapters": len(issues),
        "problem_count": sum(item.get("problem_count", 0) for item in issues),
        "issues": issues,
    }


def command_untranslated_fix(args) -> dict:
    runtime = HeadlessRuntime()
    app = runtime.bootstrap(include_engine=True)
    _settings_with_single_task_mode(args)

    project_folder = _abs_path(args.project)
    epub_path = _abs_path(args.epub)
    pm = _project_manager(project_folder)
    suffix = getattr(args, "suffix", None)
    chapters = select_chapters(
        epub_path,
        pm,
        mode=args.chapters,
        patterns=args.chapter or [],
        offset=args.offset,
        limit=args.limit,
    )
    records, missing = _load_translated_chapter_records(epub_path, project_folder, pm, chapters, suffix=suffix)
    exceptions = _word_exceptions_from_project(app.settings_manager, project_folder, getattr(args, "exceptions", None))
    data_items, soup_cache, scan_issues = _collect_untranslated_fix_items(
        records,
        word_exceptions=exceptions,
        max_context_chars=int(args.max_context_chars or 2000),
    )
    if not data_items:
        runtime.shutdown()
        return {
            "ok": True,
            "status": "no_untranslated_contexts",
            "epub": epub_path,
            "project": project_folder,
            "checked_chapters": len(records),
            "missing_translations": missing[:50],
            "issues": scan_issues,
        }

    settings = build_session_settings(app.settings_manager, pm, chapters, args)
    prompt_text = _untranslated_fix_prompt(app.settings_manager, getattr(args, "fix_prompt_file", None))
    request_payloads = _build_untranslated_fix_payloads(data_items, batch_size=args.batch_size)
    task_payloads = [
        ("raw_text_translation", payload, prompt_text, f"Untranslated fixer {index + 1}/{len(request_payloads)}")
        for index, payload in enumerate(request_payloads)
    ]
    result, task_results = _run_task_session(
        app,
        runtime,
        settings,
        task_payloads,
        verbose=bool(args.verbose),
        timeout=args.timeout,
        capture_results=True,
    )

    changes, translated_groups = _parse_untranslated_fix_changes(task_results, data_items)
    apply_info = _apply_untranslated_fix_changes(changes, soup_cache, dry_run=bool(args.dry_run)) if changes else {
        "groups_changed": 0,
        "replacements": 0,
        "affected_files": [],
        "saved_count": 0,
        "dry_run": bool(args.dry_run),
    }

    runtime.shutdown()
    return {
        "ok": _session_completed_ok(result),
        "status": "finished" if result["finished"] else "stopped",
        "epub": epub_path,
        "project": project_folder,
        "checked_chapters": len(records),
        "missing_translations": missing[:50],
        "groups_found": len(data_items),
        "translated_groups": translated_groups,
        "issues": scan_issues,
        "apply": apply_info,
        "result": result,
    }


def _add_common_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--epub", required=True, help="Source EPUB path.")
    parser.add_argument("--project", required=True, help="Translator project/output folder.")
    parser.add_argument("--chapters", choices=["pending", "all", "translated"], default="pending")
    parser.add_argument("--chapter", action="append", help="Chapter glob/substr filter. Can be repeated.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)


def _add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", help="Provider id, e.g. gemini, deepseek, openrouter, local.")
    parser.add_argument("--model", help="Model display name from config/api_providers.json.")
    parser.add_argument("--api-key", action="append", help="API key/session token. Can be repeated.")
    parser.add_argument("--api-key-file", help="Text file with one API key per line.")
    parser.add_argument("--all-keys", action="store_true", help="Use all configured keys for the provider.")
    parser.add_argument("--workers", type=int, help="Number of parallel workers.")
    parser.add_argument("--rpm", type=int, help="Per-worker/request pool RPM limit override.")
    parser.add_argument("--temperature", type=float, help="Temperature override.")
    parser.add_argument("--mode", choices=["saved", "single", "batch", "chunk", "sequential"], default="saved")
    parser.add_argument("--task-size", type=int, help="Input char limit for batch/chunk task building.")
    parser.add_argument("--splits", type=int, default=1, help="Sequential mode chain count.")
    parser.add_argument("--force-accept", action="store_true", help="Skip HTML validation rejection.")
    parser.add_argument("--json-epub", action="store_true", help="Use JSON EPUB transport pipeline.")
    parser.add_argument("--prompt-file", help="Custom translation prompt file.")
    parser.add_argument("--glossary", help="Glossary JSON path. Defaults to <project>/project_glossary.json.")
    parser.add_argument("--settings-json", help="JSON file with final settings overrides.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translator-cli",
        description="Headless CLI tools for the EPUB translator.",
    )
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    parser.add_argument("--debug", action="store_true", help="Include traceback in JSON errors.")
    settings_scope = parser.add_mutually_exclusive_group()
    settings_scope.add_argument("--settings-profile", help="Use an isolated settings/key profile.")
    settings_scope.add_argument("--settings-dir", help="Use an explicit settings directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show providers, keys, saved settings, and optional project status.")
    status.add_argument("--project")
    status.add_argument("--epub")
    status.set_defaults(func=command_status)

    providers = subparsers.add_parser("providers", help="List configured providers and key counts.")
    providers.add_argument("--all", action="store_true", help="Include hidden providers.")
    providers.add_argument(
        "--no-discovery",
        action="store_true",
        help="Do not run network model discovery. This is the default for providers.",
    )
    providers.add_argument(
        "--diagnose",
        action="store_true",
        help="Probe dynamic provider discovery endpoints and include availability diagnostics.",
    )
    providers.add_argument(
        "--doctor",
        action="store_true",
        dest="diagnose",
        help="Alias for --diagnose.",
    )
    providers.set_defaults(func=command_providers)

    models = subparsers.add_parser("models", help="List models for a provider.")
    models.add_argument("--provider", help="Provider id or display name. Defaults to saved provider.")
    models.set_defaults(func=command_models)

    settings = subparsers.add_parser("settings", help="Dump saved session settings without API keys.")
    settings.set_defaults(func=command_settings)

    plan = subparsers.add_parser("plan", help="Build a translation task plan without calling APIs.")
    _add_common_project_args(plan)
    _add_common_run_args(plan)
    plan.set_defaults(func=command_plan)

    translate = subparsers.add_parser("translate", help="Run a full headless translation session.")
    _add_common_project_args(translate)
    _add_common_run_args(translate)
    translate.add_argument("--timeout", type=int, help="Stop the session after N seconds.")
    translate.add_argument("--verbose", action="store_true", help="Mirror app log messages to stderr.")
    translate.set_defaults(func=command_translate)

    generate = subparsers.add_parser("generate", help="Run a single raw model request through the translator engine.")
    generate.add_argument("--project", help="Optional project folder for glossary/settings context.")
    generate.add_argument("--epub", help="Optional EPUB path for settings context.")
    generate.add_argument("--text", help="Input text. If omitted, --input or stdin is used.")
    generate.add_argument("--input", help="UTF-8 text file used as input.")
    generate.add_argument("--prompt", help="Prompt template. {text} is replaced with input text.")
    generate.add_argument("--label", help="Task label in logs.")
    _add_common_run_args(generate)
    generate.add_argument("--timeout", type=int, help="Stop the session after N seconds.")
    generate.add_argument("--verbose", action="store_true", help="Mirror app log messages to stderr.")
    generate.set_defaults(func=command_generate)

    glossary = subparsers.add_parser("glossary-generate", aliases=["glossary"], help="Generate AI glossary batches for selected chapters.")
    _add_common_project_args(glossary)
    _add_common_run_args(glossary)
    glossary.set_defaults(chapters="all")
    glossary.add_argument("--batch-size", type=int, default=1, help="Chapters per glossary generation task.")
    glossary.add_argument("--merge-mode", choices=["supplement", "update", "accumulate"], default="supplement")
    glossary.add_argument("--new-terms-limit", type=int, help="Limit new terms per batch.")
    glossary.add_argument("--glossary-prompt-file", help="Custom AI glossary prompt.")
    glossary.add_argument("--timeout", type=int, help="Stop the session after N seconds.")
    glossary.add_argument("--verbose", action="store_true", help="Mirror app log messages to stderr.")
    glossary.set_defaults(func=command_glossary_generate)

    consistency = subparsers.add_parser("consistency", aliases=["check-consistency"], help="Run consistency/proofread analysis on translated chapters.")
    _add_common_project_args(consistency)
    _add_common_run_args(consistency)
    consistency.set_defaults(chapters="translated")
    consistency.add_argument("--suffix", help="translation_map suffix to inspect, e.g. _validated.html.")
    consistency.add_argument("--consistency-mode", choices=["deep", "deep_consistency", "fast", "fast_proofread_3_1"], default="fast")
    consistency.add_argument("--glossary-first", action="store_true", help="Use deep two-pass glossary-first mode.")
    consistency.add_argument("--chunk-size", type=int, default=3, help="Chapters per consistency API chunk.")
    consistency.add_argument("--no-source", action="store_true", help="Do not include original EPUB source as reference.")
    consistency.add_argument("--fix", action="store_true", help="Ask AI to fix detected problems.")
    consistency.add_argument("--write", action="store_true", help="Write fixed consistency output back to translated files.")
    consistency.add_argument("--confidences", action="append", help="Problem confidence to fix. Can be repeated.")
    consistency.set_defaults(func=command_consistency)

    untranslated_scan = subparsers.add_parser("untranslated-scan", help="Scan translated chapters for untranslated residue.")
    _add_common_project_args(untranslated_scan)
    untranslated_scan.set_defaults(chapters="translated")
    untranslated_scan.add_argument("--suffix", help="translation_map suffix to inspect, e.g. _validated.html.")
    untranslated_scan.add_argument("--exceptions", help="Custom word exceptions file.")
    untranslated_scan.add_argument("--no-mixed-script", action="store_true", help="Skip mixed CJK/Cyrillic context reporting.")
    untranslated_scan.set_defaults(func=command_untranslated_scan)

    untranslated_fix = subparsers.add_parser("untranslated-fix", aliases=["fix-untranslated"], help="Use AI to fix untranslated residue in translated chapters.")
    _add_common_project_args(untranslated_fix)
    _add_common_run_args(untranslated_fix)
    untranslated_fix.set_defaults(chapters="translated")
    untranslated_fix.add_argument("--suffix", help="translation_map suffix to inspect, e.g. _validated.html.")
    untranslated_fix.add_argument("--exceptions", help="Custom word exceptions file.")
    untranslated_fix.add_argument("--fix-prompt-file", help="Custom untranslated-fixer prompt.")
    untranslated_fix.add_argument("--batch-size", type=int, default=50, help="Contexts per raw fixer request.")
    untranslated_fix.add_argument("--max-context-chars", type=int, default=2000)
    untranslated_fix.add_argument("--dry-run", action="store_true", help="Run AI and report changes without writing files.")
    untranslated_fix.add_argument("--timeout", type=int, help="Stop the session after N seconds.")
    untranslated_fix.add_argument("--verbose", action="store_true", help="Mirror app log messages to stderr.")
    untranslated_fix.set_defaults(func=command_untranslated_fix)

    build_epub = subparsers.add_parser("build-epub", help="Build an EPUB by replacing source chapters with translated files.")
    build_epub.add_argument("--epub", required=True)
    build_epub.add_argument("--project", required=True)
    build_epub.add_argument("--output")
    build_epub.add_argument("--provider", help="Use this provider's file suffix.")
    build_epub.add_argument("--suffix", help="Explicit translation_map suffix to select.")
    build_epub.add_argument("--chapter", action="append")
    build_epub.add_argument("--offset", type=int, default=0)
    build_epub.add_argument("--limit", type=int)
    build_epub.add_argument("--strict", action="store_true", help="Fail if any selected source chapter has no translation.")
    build_epub.set_defaults(func=command_build_epub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "settings_dir", None):
        from .utils.settings import configure_settings_scope

        configure_settings_scope(config_dir=args.settings_dir)
    elif getattr(args, "settings_profile", None) is not None:
        from .utils.settings import configure_settings_scope

        configure_settings_scope(profile=args.settings_profile)
    try:
        with _redirect_internal_stdout_to_stderr():
            payload = args.func(args)
        _write_json(payload, pretty=not args.compact)
        return 0 if payload.get("ok", True) else 1
    except CliError as exc:
        payload = {"ok": False, "error": str(exc)}
        payload.update(exc.payload)
        if getattr(args, "debug", False):
            payload["traceback"] = traceback.format_exc()
        _write_json(payload, pretty=not args.compact)
        return exc.exit_code
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if getattr(args, "debug", False):
            payload["traceback"] = traceback.format_exc()
        _write_json(payload, pretty=not args.compact)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
