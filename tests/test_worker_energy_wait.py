import asyncio
import time
import types
import unittest

from gemini_translator.core.worker import UniversalWorker


class WorkerEnergyWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_wait_wakes_on_notification(self):
        worker = types.SimpleNamespace(_wake_event=asyncio.Event())

        async def wake_worker():
            await asyncio.sleep(0.01)
            worker._wake_event.set()

        start = time.perf_counter()
        await asyncio.gather(
            UniversalWorker._wait_for_next_cycle(worker, set(), timeout=1.0),
            wake_worker(),
        )
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5)
        self.assertFalse(worker._wake_event.is_set())

    async def test_active_task_completion_wakes_loop(self):
        worker = types.SimpleNamespace(_wake_event=asyncio.Event())

        async def finish_quickly():
            await asyncio.sleep(0.01)

        task = asyncio.create_task(finish_quickly())

        start = time.perf_counter()
        await UniversalWorker._wait_for_next_cycle(worker, {task}, timeout=1.0)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5)
        self.assertTrue(task.done())
