import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from gemini_translator.core.translation_engine import TranslationEngine


class _TopicOnlyBus:
    def __init__(self):
        self.subscriptions = {}

    def subscribe(self, event_name, callback):
        self.subscriptions.setdefault(event_name, []).append(callback)

    def unsubscribe(self, event_name, callback):
        callbacks = self.subscriptions.get(event_name, [])
        if callback in callbacks:
            callbacks.remove(callback)


class _DummyContext:
    chinese_processor = None


class _DummySettings:
    pass


class _DummyTaskManager:
    pass


class CoreTopicSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_translation_engine_subscribes_only_to_relevant_topics(self):
        bus = _TopicOnlyBus()

        engine = TranslationEngine(
            context_manager=_DummyContext(),
            settings_manager=_DummySettings(),
            task_manager=_DummyTaskManager(),
            event_bus=bus,
        )

        self.addCleanup(engine.cleanup)
        self.assertIn("start_session_requested", bus.subscriptions)
        self.assertIn("temporary_limit_warning_received", bus.subscriptions)
        self.assertIn("task_finished", bus.subscriptions)
        self.assertNotIn("log_message", bus.subscriptions)
