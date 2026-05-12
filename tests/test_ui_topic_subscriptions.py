import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from gemini_translator.ui.widgets.log_widget import LogWidget
from gemini_translator.ui.widgets.status_bar_widget import StatusBarWidget
from gemini_translator.ui.widgets.task_management_widget import TaskManagementWidget


class _TopicOnlyBus:
    def __init__(self):
        self.subscriptions = {}
        self.unsubscriptions = []

    def subscribe(self, event_name, callback):
        self.subscriptions.setdefault(event_name, []).append(callback)

    def unsubscribe(self, event_name, callback):
        self.unsubscriptions.append((event_name, callback))
        callbacks = self.subscriptions.get(event_name, [])
        if callback in callbacks:
            callbacks.remove(callback)

    def emit(self, event_name, data=None):
        event = {"event": event_name, "data": data or {}}
        for callback in list(self.subscriptions.get(event_name, [])):
            callback(event)


class UiTopicSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_log_widget_uses_topic_subscription(self):
        bus = _TopicOnlyBus()
        widget = LogWidget(event_bus=bus)
        try:
            self.assertIn("log_message", bus.subscriptions)

            bus.emit("log_message", {"message": "[INFO] topic log"})
            widget._flush_pending_messages()

            self.assertIn("topic log", widget.log_view.toPlainText())
        finally:
            widget.close()
            widget.deleteLater()

    def test_status_bar_uses_topic_subscriptions(self):
        bus = _TopicOnlyBus()
        widget = StatusBarWidget(event_bus=bus, engine=None)
        try:
            self.assertIn("session_started", bus.subscriptions)
            self.assertIn("session_finished", bus.subscriptions)
            self.assertIn("task_state_changed", bus.subscriptions)

            bus.emit("session_started", {"total_tasks": 2})
            self.assertEqual(widget.total_tasks, 2)
        finally:
            widget.close()
            widget.deleteLater()

    def test_task_management_widget_uses_topic_subscriptions(self):
        bus = _TopicOnlyBus()
        old_bus = getattr(self.app, "event_bus", None)
        self.app.event_bus = bus
        try:
            widget = TaskManagementWidget()
            try:
                self.assertIn("task_state_changed", bus.subscriptions)
                self.assertIn("session_finished", bus.subscriptions)
            finally:
                widget.close()
                widget.deleteLater()
        finally:
            if old_bus is None:
                delattr(self.app, "event_bus")
            else:
                self.app.event_bus = old_bus


if __name__ == "__main__":
    unittest.main()
