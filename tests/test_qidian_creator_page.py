import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets, sip
from PyQt6.QtGui import QPixmap

from gemini_translator.ui.pages.qidian_creator_page import (
    QIDIAN_CREATOR_UI_STATE_KEY,
    QidianCreatorPage,
    _CoverDropLabel,
)
from qidian_rulate.models import DEFAULT_RULATE_TELEGRAM_LINK, DEFAULT_RULATE_VK_LINK
from gemini_translator.ui.dialogs.qidian_rulate_creator import _split_csv
from gemini_translator.ui.shell import ShellPage


class _TextField:
    def __init__(self):
        self.value = None

    def setText(self, value):
        self.value = value

    def setPlainText(self, value):
        self.value = value

    def text(self):
        return self.value or ""


class _ButtonField:
    def __init__(self):
        self.enabled = None
        self.hidden = False
        self.text = ""

    def setEnabled(self, value):
        self.enabled = value

    def setVisible(self, value):
        self.hidden = not value

    def setText(self, value):
        self.text = value


class _ComboField:
    def __init__(self):
        self.data = ["first_suggestion", ""]
        self.find_calls = []
        self.index = None
        self.current_index = 0

    def findData(self, value):
        self.find_calls.append(value)
        try:
            return self.data.index(value)
        except ValueError:
            return -1

    def setCurrentIndex(self, index):
        self.index = index
        self.current_index = index

    def currentData(self):
        return self.data[self.current_index]


class _SettingsManagerStub:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.saved_ui_state = None

    def load_settings(self):
        return dict(self.settings)

    def save_ui_state(self, payload):
        self.saved_ui_state = payload
        self.settings.update(payload)
        return True


class _ApplyPreparedMetadataHarness:
    _apply_prepared_metadata = QidianCreatorPage._apply_prepared_metadata

    def __init__(self):
        self.english_title_edit = _TextField()
        self.translated_title_edit = _TextField()
        self.translated_description_edit = _TextField()
        self.genres_edit = _TextField()
        self.tags_edit = _TextField()
        self.translator_team_combo = _ComboField()
        self.cover_prompt_edit = _TextField()
        self.action_state_updated = False

    def _update_action_state(self):
        self.action_state_updated = True


class _UiStateHarness:
    _load_ui_state = QidianCreatorPage._load_ui_state
    _save_ui_state = QidianCreatorPage._save_ui_state

    def __init__(self, settings=None):
        self.settings_manager = _SettingsManagerStub(settings)
        self.translator_team_combo = _ComboField()
        self.vk_link_edit = _TextField()
        self.vk_link_edit.setText(DEFAULT_RULATE_VK_LINK)
        self.telegram_link_edit = _TextField()
        self.telegram_link_edit.setText(DEFAULT_RULATE_TELEGRAM_LINK)


class _WorkerFinishedHarness:
    _worker_finished = QidianCreatorPage._worker_finished
    _set_button_enabled = QidianCreatorPage._set_button_enabled
    _set_prepare_ai_running = QidianCreatorPage._set_prepare_ai_running
    _update_action_state = QidianCreatorPage._update_action_state

    def __init__(self, worker):
        self._workers = [worker]
        self._prepare_ai_worker = None
        self._prepare_ai_cancel_requested = False
        self.prepare_ai_btn = None
        self.cancel_prepare_ai_btn = None
        self.login_rulate_btn = None
        self.fill_rulate_btn = None


class _CoverFolderButtonHarness:
    _set_button_enabled = QidianCreatorPage._set_button_enabled
    _set_codex_cover_folder_button_enabled = QidianCreatorPage._set_codex_cover_folder_button_enabled

    def __init__(self, cover_path):
        self._generated_cover_path = str(cover_path)
        self.open_codex_cover_folder_btn = _ButtonField()


class _DroppedCoverHarness:
    _apply_dropped_codex_cover = QidianCreatorPage._apply_dropped_codex_cover

    def __init__(self):
        self._generated_cover_path = ""
        self.preview_path = None
        self.action_state_updated = False
        self.logs = []

    def _set_codex_cover_preview(self, image_path):
        self.preview_path = image_path

    def _update_action_state(self):
        self.action_state_updated = True

    def _log(self, level, message):
        self.logs.append((level, message))


class _DroppedSourceCoverHarness:
    _apply_dropped_source_cover = QidianCreatorPage._apply_dropped_source_cover

    def __init__(self):
        self._local_source_cover_path = ""
        self.preview_data = None
        self.logs = []

    def _set_cover_preview(self, image_data):
        self.preview_data = image_data

    def _log(self, level, message):
        self.logs.append((level, message))


class _CancelablePrepareWorker:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _PrepareAiButtonHarness:
    _cancel_prepare_ai = QidianCreatorPage._cancel_prepare_ai
    _set_prepare_ai_running = QidianCreatorPage._set_prepare_ai_running

    def __init__(self):
        self._prepare_ai_worker = _CancelablePrepareWorker()
        self._prepare_ai_cancel_requested = False
        self.prepare_ai_btn = _ButtonField()
        self.cancel_prepare_ai_btn = _ButtonField()
        self.logs = []

    def _log(self, level, message):
        self.logs.append((level, message))


class QidianCreatorPageContractTests(unittest.TestCase):
    def test_is_shell_page_subclass(self):
        self.assertTrue(issubclass(QidianCreatorPage, ShellPage))

    def test_page_title(self):
        self.assertEqual(QidianCreatorPage.page_title, "Qidian/Fanqie/Ciweimao/Qimao → Rulate")

    def test_split_csv_dedupes_and_strips(self):
        self.assertEqual(_split_csv("a, b ,a\nc"), ["a", "b", "c"])
        self.assertEqual(_split_csv(""), [])

    def test_log_is_in_dedicated_tab(self):
        page_source = Path("gemini_translator/ui/pages/qidian_creator_page.py").read_text(encoding="utf-8")

        self.assertIn("self.main_tabs = QTabWidget()", page_source)
        self.assertIn('self.main_tabs.addTab(main_tab, "Основное")', page_source)
        self.assertIn('self.main_tabs.addTab(log_tab, "Лог")', page_source)
        self.assertNotIn("root.addWidget(log_group)", page_source)

    def test_apply_prepared_metadata_accepts_legacy_metadata_without_translator_team_mode(self):
        page = _ApplyPreparedMetadataHarness()
        prepared = SimpleNamespace(
            english_title="Otherworldly Inn",
            translated_title="Inn",
            translated_description="Description",
            genres=["fantasy", "adventure"],
            tags=["tag-one", "tag-two"],
            cover_prompt="",
        )

        page._apply_prepared_metadata(prepared)

        self.assertEqual(page.english_title_edit.value, "Otherworldly Inn")
        self.assertEqual(page.genres_edit.value, "fantasy, adventure")
        self.assertEqual(page.translator_team_combo.find_calls, [])
        self.assertTrue(page.action_state_updated)

    def test_load_ui_state_restores_translator_team_mode(self):
        page = _UiStateHarness(
            {
                QIDIAN_CREATOR_UI_STATE_KEY: {
                    "translator_team_mode": "",
                }
            }
        )

        page._load_ui_state()

        self.assertEqual(page.translator_team_combo.currentData(), "")

    def test_save_ui_state_persists_translator_team_mode(self):
        page = _UiStateHarness()
        page.translator_team_combo.setCurrentIndex(1)

        page._save_ui_state()

        self.assertEqual(
            page.settings_manager.saved_ui_state,
            {
                QIDIAN_CREATOR_UI_STATE_KEY: {
                    "translator_team_mode": "",
                    "vk_link": DEFAULT_RULATE_VK_LINK,
                    "telegram_link": DEFAULT_RULATE_TELEGRAM_LINK,
                }
            },
        )

    def test_worker_finished_ignores_deleted_button(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        _ = app
        worker = object()
        button = QtWidgets.QPushButton()
        page = _WorkerFinishedHarness(worker)

        sip.delete(button)

        page._worker_finished(worker, button)

        self.assertEqual(page._workers, [])

    def test_prepare_ai_cancel_button_replaces_prepare_button(self):
        page = _PrepareAiButtonHarness()

        page._set_prepare_ai_running(True)

        self.assertTrue(page.prepare_ai_btn.hidden)
        self.assertFalse(page.cancel_prepare_ai_btn.hidden)
        self.assertTrue(page.cancel_prepare_ai_btn.enabled)

    def test_cancel_prepare_ai_requests_worker_cancel_and_disables_button(self):
        page = _PrepareAiButtonHarness()
        worker = page._prepare_ai_worker

        page._cancel_prepare_ai()

        self.assertTrue(worker.cancelled)
        self.assertTrue(page._prepare_ai_cancel_requested)
        self.assertFalse(page.cancel_prepare_ai_btn.enabled)
        self.assertEqual(page.cancel_prepare_ai_btn.text, "Отмена...")

    def test_cover_folder_button_is_enabled_only_for_existing_cover(self):
        cover_path = Path(os.environ.get("TEMP", ".")) / "codex-cover-button-test.png"
        cover_path.write_bytes(b"image")
        try:
            page = _CoverFolderButtonHarness(cover_path)
            page._set_codex_cover_folder_button_enabled()
            self.assertTrue(page.open_codex_cover_folder_btn.enabled)

            page._generated_cover_path = str(cover_path.with_name("missing.png"))
            page._set_codex_cover_folder_button_enabled()
            self.assertFalse(page.open_codex_cover_folder_btn.enabled)
        finally:
            cover_path.unlink(missing_ok=True)

    def test_codex_cover_preview_label_accepts_file_drops(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        _ = app
        label = _CoverDropLabel()

        self.assertTrue(label.acceptDrops())

    def test_dropped_cover_becomes_current_generated_cover(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        _ = app
        cover_path = Path(os.environ.get("TEMP", ".")) / "codex-cover-drop-test.png"
        pixmap = QPixmap(1, 1)
        self.assertTrue(pixmap.save(str(cover_path), "PNG"))
        try:
            page = _DroppedCoverHarness()
            page._apply_dropped_codex_cover(str(cover_path))

            self.assertEqual(page._generated_cover_path, str(cover_path.resolve()))
            self.assertEqual(page.preview_path, str(cover_path.resolve()))
            self.assertTrue(page.action_state_updated)
            self.assertTrue(any(level == "INFO" for level, _message in page.logs))
        finally:
            cover_path.unlink(missing_ok=True)

    def test_dropped_source_cover_becomes_local_translation_source(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        _ = app
        cover_path = Path(os.environ.get("TEMP", ".")) / "source-cover-drop-test.png"
        pixmap = QPixmap(1, 1)
        self.assertTrue(pixmap.save(str(cover_path), "PNG"))
        try:
            page = _DroppedSourceCoverHarness()
            page._apply_dropped_source_cover(str(cover_path))

            self.assertEqual(page._local_source_cover_path, str(cover_path.resolve()))
            self.assertTrue(page.preview_data)
            self.assertTrue(any(level == "INFO" for level, _message in page.logs))
        finally:
            cover_path.unlink(missing_ok=True)
