import os
import tempfile
import unittest
import zipfile

from gemini_translator.api.errors import PartialGenerationError, ValidationFailedError, WorkerAction
from gemini_translator.core.worker_helpers.error_analyzer import ErrorAnalyzer
from gemini_translator.core.worker_helpers.emerger_tasks import EmergencyTask


class _DummyTaskManager:
    def __init__(self):
        self.failures = []

    def _get_task_display_name(self, payload):
        return payload[2] if len(payload) > 2 else str(payload)

    def record_failure(self, task_info, error_type):
        self.failures.append(error_type)

    def get_failure_history(self, task_info):
        counts = {}
        for error_type in self.failures:
            counts[error_type] = counts.get(error_type, 0) + 1
        return {"total_count": len(self.failures), "errors": counts}


class _DummyWorker:
    def __init__(self):
        self.task_manager = _DummyTaskManager()
        self.events = []
        self.chunking = False
        self.chunk_on_error = False

    def _post_event(self, name, data=None):
        self.events.append((name, data or {}))


class ErrorAnalyzerRetryTests(unittest.TestCase):
    def test_validation_errors_retry_beyond_default_total_limit(self):
        worker = _DummyWorker()
        analyzer = ErrorAnalyzer(worker)
        task_info = ("task-id", ("epub", "book.epub", "Text/ch.xhtml"))
        history = {"total_count": 4, "errors": {"VALIDATION": 4}}

        action, error_type, _ = analyzer.analyze_and_act(
            ValidationFailedError("invalid html"),
            task_info,
            history,
        )

        self.assertEqual(action, WorkerAction.RETRY_COUNTABLE)
        self.assertEqual(error_type.name, "VALIDATION")
        self.assertEqual(worker.task_manager.failures, ["VALIDATION"])

    def test_validation_errors_fail_on_sixth_invalid_response(self):
        worker = _DummyWorker()
        analyzer = ErrorAnalyzer(worker)
        task_info = ("task-id", ("epub", "book.epub", "Text/ch.xhtml"))
        history = {"total_count": 5, "errors": {"VALIDATION": 5}}

        action, error_type, _ = analyzer.analyze_and_act(
            ValidationFailedError("invalid html"),
            task_info,
            history,
        )

        self.assertEqual(action, WorkerAction.FAIL_PERMANENTLY)
        self.assertEqual(error_type.name, "VALIDATION")

    def test_partial_completion_keeps_the_task_a_whole_chapter(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", ("epub", "book.epub", "Text/ch.xhtml"))
        exc = PartialGenerationError("partial", "<p>translated</p>", "MAX_TOKENS")

        task_id, payload = emerger._mutate_task_for_completion(task_info, exc)

        # Глава остаётся главой: раньше её переодевали в 'epub_chunk' 0/1,
        # и в списке задач она навсегда становилась «ЧАНК 1/1».
        self.assertEqual(task_id, "task-id")
        self.assertEqual(payload, ("epub", "book.epub", "Text/ch.xhtml", "<p>translated</p>"))

    def test_partial_completion_does_not_depend_on_chunk_settings(self):
        worker = _DummyWorker()
        worker.chunking = False
        worker.chunk_on_error = False
        emerger = EmergencyTask(worker)
        task_info = ("task-id", ("epub", "book.epub", "Text/ch.xhtml"))
        exc = PartialGenerationError("partial", "<p>translated</p>", "MAX_TOKENS")

        _, payload = emerger._mutate_task_for_completion(task_info, exc)

        self.assertEqual(payload[0], "epub")
        self.assertEqual(payload[3], "<p>translated</p>")

    def test_second_truncation_extends_the_chapter_partial(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        head = (
            "<p>Первый абзац главы, с которого начинается перевод: герой выходит из дома "
            "и щурится на утреннее солнце, вспоминая вчерашний разговор.</p>"
        )
        tail = "\n<p>Продолжение, дописанное во второй попытке.</p>"
        task_info = ("task-id", ("epub", "book.epub", "Text/ch.xhtml", head))

        _, payload = emerger._mutate_task_for_completion(
            task_info, PartialGenerationError("partial", tail, "MAX_TOKENS")
        )

        self.assertEqual(len(payload), 4)
        self.assertTrue(payload[3].startswith(head))
        self.assertIn("Продолжение", payload[3])


if __name__ == "__main__":
    unittest.main()
