import unittest

from main import EventBus


class EventBusTopicTests(unittest.TestCase):
    def test_emit_event_delivers_to_topic_and_legacy_subscribers(self):
        bus = EventBus()
        topic_events = []
        legacy_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.event_posted.connect(legacy_events.append)

        event = {"event": "log_message", "data": {"message": "hello"}}
        bus.emit_event(event)

        self.assertEqual(topic_events, [event])
        self.assertEqual(legacy_events, [event])

    def test_topic_subscriber_only_receives_matching_event(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)

        bus.emit_event({"event": "task_state_changed", "data": {}})

        self.assertEqual(topic_events, [])

    def test_unsubscribe_removes_topic_callback(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.unsubscribe("log_message", topic_events.append)

        bus.emit_event({"event": "log_message", "data": {"message": "hello"}})

        self.assertEqual(topic_events, [])

    def test_unsubscribe_all_removes_callback_from_every_topic(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.subscribe("session_finished", topic_events.append)
        bus.unsubscribe_all(topic_events.append)

        bus.emit_event({"event": "log_message", "data": {}})
        bus.emit_event({"event": "session_finished", "data": {}})

        self.assertEqual(topic_events, [])


if __name__ == "__main__":
    unittest.main()
