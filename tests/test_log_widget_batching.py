import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from gemini_translator.ui.widgets.log_widget import (
    LOG_FLUSH_INTERVAL_MS,
    MAX_LOG_FLUSH_BATCH_SIZE,
    LogWidget,
)


class LogWidgetBatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_append_message_queues_until_flush(self):
        widget = LogWidget(event_bus=None)
        try:
            widget.append_message({"message": "[INFO] one"})

            self.assertEqual(len(widget._pending_log_data), 1)
            self.assertNotIn("one", widget.log_view.toPlainText())

            widget._flush_pending_messages()

            self.assertEqual(widget._pending_log_data, [])
            self.assertIn("one", widget.log_view.toPlainText())
        finally:
            widget.deleteLater()

    def test_flush_inserts_multiple_messages_as_one_batch(self):
        widget = LogWidget(event_bus=None)
        inserted_batches = []
        widget._insert_html_batch = inserted_batches.append

        try:
            widget.append_message({"message": "[INFO] first"})
            widget.append_message({"message": "[WARN] second"})

            widget._flush_pending_messages()

            self.assertEqual(len(inserted_batches), 1)
            self.assertIn("first", inserted_batches[0])
            self.assertIn("second", inserted_batches[0])
        finally:
            widget.deleteLater()

    def test_flush_yields_between_large_batches(self):
        widget = LogWidget(event_bus=None)
        inserted_batches = []
        scheduled_delays = []
        widget._insert_html_batch = inserted_batches.append
        widget._schedule_log_flush = scheduled_delays.append

        try:
            for index in range(MAX_LOG_FLUSH_BATCH_SIZE + 1):
                widget._queue_log_message({"message": f"[INFO] item {index}"})

            widget._flush_pending_messages()

            self.assertEqual(len(inserted_batches), 1)
            self.assertEqual(len(widget._pending_log_data), 1)
            self.assertEqual(scheduled_delays, [LOG_FLUSH_INTERVAL_MS])
        finally:
            widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
