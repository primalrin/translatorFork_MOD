import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from gemini_translator.ui.widgets.status_bar_widget import StatusBarWidget


class _ExplodingTaskManager:
    def get_ui_state_list(self):
        raise AssertionError("status bar should use the event payload, not re-query the task manager")


class _DummyEngine:
    task_manager = _ExplodingTaskManager()


class StatusBarThrottlingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_task_state_changed_is_deferred_and_uses_payload(self):
        widget = StatusBarWidget(event_bus=None, engine=_DummyEngine())
        try:
            widget.start_session(3)
            widget.on_event({
                "event": "task_state_changed",
                "data": {
                    "full_state": [
                        ("task-1", "success", {}),
                        ("task-2", "in_progress", {}),
                        ("task-3", "filtered", {}),
                    ]
                },
            })

            self.assertEqual(widget.success_count, 0)
            self.assertEqual(widget.in_progress_count, 0)
            self.assertEqual(widget.filtered_count, 0)

            widget._flush_pending_task_state()

            self.assertEqual(widget.success_count, 1)
            self.assertEqual(widget.in_progress_count, 1)
            self.assertEqual(widget.filtered_count, 1)
        finally:
            widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
