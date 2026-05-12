import os
import sqlite3
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from gemini_translator.api import config as api_config
from gemini_translator.core.task_manager import ChapterQueueManager


class _RecordingBus(QtCore.QObject):
    event_posted = QtCore.pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.events = []
        self.event_posted.connect(self.events.append)


class TaskManagerTasksAddedEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.main_db_connection = sqlite3.connect(
            api_config.SHARED_DB_URI,
            uri=True,
            check_same_thread=False,
        )
        cls.app.main_db_connection.row_factory = sqlite3.Row

    def setUp(self):
        self.bus = _RecordingBus()
        self.app.event_bus = self.bus
        self.manager = ChapterQueueManager(event_bus=self.bus)
        self.manager.clear_all_queues()
        self.manager.session_id = "session-1"

    def test_set_pending_tasks_emits_tasks_added_for_worker_wakeup(self):
        self.manager.set_pending_tasks([("hello_task",)])

        matching_events = [
            event for event in self.bus.events
            if event.get("event") == "tasks_added"
        ]
        self.assertEqual(len(matching_events), 1)
        self.assertEqual(matching_events[0].get("session_id"), "session-1")
        self.assertEqual(matching_events[0].get("data", {}).get("count"), 1)
        self.assertEqual(matching_events[0].get("data", {}).get("reason"), "set_pending_tasks")
