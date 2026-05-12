import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gemini_translator.ui.dialogs.setup import InitialSetupDialog


class _TaskManagerStub:
    def __init__(self, has_pending=False):
        self.has_pending = has_pending

    def has_pending_tasks(self):
        return self.has_pending


class _EngineStub:
    def __init__(self, task_manager):
        self.task_manager = task_manager


class _StartTranslationHarness:
    _ensure_pending_tasks_for_start = InitialSetupDialog._ensure_pending_tasks_for_start

    def __init__(self, *, has_pending=False, html_files=None):
        self.task_manager = _TaskManagerStub(has_pending=has_pending)
        self.engine = _EngineStub(self.task_manager)
        self.selected_file = "/tmp/book.epub"
        self.output_folder = "/tmp/book"
        self.html_files = list(["Text/chapter.xhtml"] if html_files is None else html_files)
        self.prepare_calls = []

    def _prepare_and_display_tasks(self, clean_rebuild=False, translation_options_override=None):
        self.prepare_calls.append(
            {
                "clean_rebuild": bool(clean_rebuild),
                "translation_options_override": translation_options_override,
            }
        )
        self.task_manager.has_pending = True


class StartTranslationQueueTests(unittest.TestCase):
    def test_start_translation_rebuilds_missing_task_queue_from_selected_chapters(self):
        harness = _StartTranslationHarness(has_pending=False)

        self.assertIs(harness._ensure_pending_tasks_for_start(), True)

        self.assertEqual(
            harness.prepare_calls,
            [
                {
                    "clean_rebuild": True,
                    "translation_options_override": None,
                }
            ],
        )

    def test_start_translation_does_not_rebuild_when_pending_tasks_exist(self):
        harness = _StartTranslationHarness(has_pending=True)

        self.assertIs(harness._ensure_pending_tasks_for_start(), True)

        self.assertEqual(harness.prepare_calls, [])

    def test_start_translation_does_not_rebuild_without_selected_chapters(self):
        harness = _StartTranslationHarness(has_pending=False, html_files=[])

        self.assertIs(harness._ensure_pending_tasks_for_start(), False)

        self.assertEqual(harness.prepare_calls, [])


if __name__ == "__main__":
    unittest.main()
