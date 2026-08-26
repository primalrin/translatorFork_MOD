import unittest
from unittest.mock import MagicMock

from gemini_translator.core.translation_engine import TranslationEngine


class WarningPenaltyTests(unittest.TestCase):
    def _engine(self, *, request_count):
        engine = TranslationEngine.__new__(TranslationEngine)
        engine.active_workers_map = {"worker-1": object()}
        engine.keys_map = {"worker-1": "gemini-secret-key"}
        engine.session_id = "session-1"
        engine.session_settings = {"rpd_limit": 500}
        engine.key_warning_counters = {}
        engine.settings_manager = MagicMock()
        engine.settings_manager.get_key_info.return_value = {
            "key": "gemini-secret-key",
            "provider": "gemini",
        }
        engine.settings_manager.get_request_count.return_value = request_count
        engine.bus = MagicMock()
        engine._post_event = MagicMock()
        return engine

    def test_repeated_temporary_limits_do_not_mark_daily_quota_exhausted(self):
        engine = self._engine(request_count=416)

        results = [
            engine._apply_warning_penalty(
                "worker-1",
                "gemini-3.5-flash-lite",
                worker_session="session-1",
            )
            for _ in range(engine.MAX_REPEATED_WAITS)
        ]

        self.assertEqual(results, [False] * engine.MAX_REPEATED_WAITS)
        self.assertEqual(
            engine.key_warning_counters["worker-1"],
            engine.MAX_REPEATED_WAITS,
        )
        engine.bus.event_posted.emit.assert_not_called()

    def test_warning_near_rpd_limit_still_marks_quota_exhausted(self):
        engine = self._engine(request_count=450)

        exhausted = engine._apply_warning_penalty(
            "worker-1",
            "gemini-3.5-flash-lite",
            worker_session="session-1",
        )

        self.assertTrue(exhausted)
        event = engine.bus.event_posted.emit.call_args.args[0]
        self.assertEqual(event["event"], "fatal_error")
        self.assertEqual(event["data"]["payload"]["type"], "quota_exceeded")


if __name__ == "__main__":
    unittest.main()
