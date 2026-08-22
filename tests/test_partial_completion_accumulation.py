# -*- coding: utf-8 -*-
"""
Регрессия на «главу-зомби ЧАНК 1/1».

Оборванный ответ превращает задачу 'epub' в 'epub_chunk' 0/1 с хвостом перевода
в payload[8]. Если следующая попытка тоже обрывается, PartialGenerationError
несёт ТОЛЬКО свежесгенерированный кусок (продолжение), а не весь накопленный
перевод. Раньше этот кусок затирал накопленное — в payload оставался фрагмент
из середины главы, и любая следующая до-генерация давала «середина + хвост»,
который валидатор сравнивал с целой главой и заваливал. Итог: 6 подряд
VALIDATION-ошибок и глава, навсегда застрявшая в списке как «ЧАНК 1/1».
"""

import unittest

from gemini_translator.api.errors import PartialGenerationError, ValidationFailedError
from gemini_translator.core.worker_helpers.emerger_tasks import EmergencyTask
from gemini_translator.utils.text import merge_partial_with_overlap_guard


# Абзацы намеренно длиннее «пробы начала» (120 символов) — на коротких огрызках
# распознавание перезапуска перевода не срабатывает, а в живых главах его хватает.
CHAPTER_HEAD = (
    "<p>Первый абзац главы, с которого начинается перевод: здесь герой выходит из дома, "
    "щурится на утреннее солнце и вспоминает вчерашний разговор с отцом.</p>"
)
CHAPTER_MIDDLE = (
    "\n<p>Середина главы, куда модель добралась во второй попытке: разговор в трактире "
    "затягивается, за окном темнеет, а решение так и не принято.</p>"
)
CHAPTER_TAIL = (
    "\n<p>Хвост главы, дописанный в третьей попытке: дверь захлопывается, шаги стихают, "
    "и на столе остаётся только недопитая кружка.</p>"
)
DEGENERATE_TAIL = "<p>─ …</p>\n" * 6018


class _DummyWorker:
    def __init__(self):
        self.events = []
        self.chunking = True
        self.chunk_on_error = False

    def _post_event(self, name, data=None):
        self.events.append((name, data or {}))

    def log_messages(self):
        return [data.get("message", "") for name, data in self.events if name == "log_message"]


def _chunk_payload(*, total_chunks=1, chunk_index=0, partial=None):
    payload = [
        "epub_chunk",
        "book.epub",
        "Text/ch.xhtml",
        "<p>source</p>",
        chunk_index,
        total_chunks,
        "<html><body>",
        "</body></html>",
    ]
    if partial is not None:
        payload.append(partial)
    return tuple(payload)


def _truncated(text):
    return PartialGenerationError("truncated", partial_text=text, reason="MAX_TOKENS")


class MergePartialTests(unittest.TestCase):
    def test_continuation_is_appended(self):
        merged, overlap = merge_partial_with_overlap_guard(CHAPTER_HEAD, CHAPTER_MIDDLE)

        self.assertEqual(merged, CHAPTER_HEAD + CHAPTER_MIDDLE)
        self.assertEqual(overlap, 0)

    def test_repeated_tail_is_deduplicated(self):
        previous = CHAPTER_HEAD + CHAPTER_MIDDLE
        new_text = CHAPTER_MIDDLE + CHAPTER_TAIL

        merged, overlap = merge_partial_with_overlap_guard(previous, new_text)

        self.assertEqual(merged, CHAPTER_HEAD + CHAPTER_MIDDLE + CHAPTER_TAIL)
        self.assertGreater(overlap, 0)

    def test_full_restart_replaces_instead_of_duplicating_the_head(self):
        previous = CHAPTER_HEAD + CHAPTER_MIDDLE
        new_text = CHAPTER_HEAD + CHAPTER_MIDDLE + CHAPTER_TAIL

        merged, _ = merge_partial_with_overlap_guard(previous, new_text)

        self.assertEqual(merged, new_text)
        self.assertEqual(merged.count(CHAPTER_HEAD), 1)

    def test_empty_sides_are_passed_through(self):
        self.assertEqual(merge_partial_with_overlap_guard("", CHAPTER_HEAD), (CHAPTER_HEAD, 0))
        self.assertEqual(merge_partial_with_overlap_guard(CHAPTER_HEAD, ""), (CHAPTER_HEAD, 0))


class AccumulatePartialTests(unittest.TestCase):
    def test_second_truncation_extends_the_stored_partial(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(partial=CHAPTER_HEAD))

        _, payload = emerger._mutate_task_for_completion(task_info, _truncated(CHAPTER_MIDDLE))

        self.assertEqual(len(payload), 9)
        self.assertTrue(
            payload[8].startswith(CHAPTER_HEAD),
            "накопленный перевод обязан начинаться с начала главы",
        )
        self.assertIn(CHAPTER_MIDDLE, payload[8])

    def test_third_truncation_keeps_growing_the_prefix(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)

        _, payload = emerger._mutate_task_for_completion(
            ("task-id", _chunk_payload(partial=CHAPTER_HEAD)),
            _truncated(CHAPTER_MIDDLE),
        )
        _, payload = emerger._mutate_task_for_completion(
            ("task-id", payload),
            _truncated(CHAPTER_TAIL),
        )

        self.assertEqual(payload[8], CHAPTER_HEAD + CHAPTER_MIDDLE + CHAPTER_TAIL)

    def test_first_truncation_stores_the_partial_as_is(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(total_chunks=2, chunk_index=1))

        _, payload = emerger._mutate_task_for_completion(task_info, _truncated(CHAPTER_HEAD))

        self.assertEqual(len(payload), 9)
        self.assertEqual(payload[8], CHAPTER_HEAD)


class RestoreWholeChapterTests(unittest.TestCase):
    def test_useless_continuation_restores_a_plain_chapter_task(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        previous = CHAPTER_HEAD + CHAPTER_MIDDLE
        task_info = ("task-id", _chunk_payload(partial=previous))

        _, payload = emerger._mutate_task_for_completion(task_info, _truncated(CHAPTER_HEAD))

        self.assertEqual(payload, ("epub", "book.epub", "Text/ch.xhtml"))

    def test_degenerate_partial_restores_a_plain_chapter_task(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(partial=CHAPTER_HEAD))

        _, payload = emerger._mutate_task_for_completion(task_info, _truncated(DEGENERATE_TAIL))

        self.assertEqual(payload, ("epub", "book.epub", "Text/ch.xhtml"))

    def test_unconvergent_partial_is_dropped_after_repeated_validation_failures(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        # Хвост из середины главы — наследство старых запусков, до-генерация
        # с ним не сойдётся никогда.
        task_info = ("task-id", _chunk_payload(partial=CHAPTER_MIDDLE))
        history = {"total_count": 2, "errors": {"VALIDATION": 2}}

        _, payload = emerger._mutate_task_for_completion(
            task_info, ValidationFailedError("не прошло валидацию"), history
        )

        self.assertEqual(payload, ("epub", "book.epub", "Text/ch.xhtml"))

    def test_partial_survives_the_first_validation_failure(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(partial=CHAPTER_MIDDLE))
        history = {"total_count": 1, "errors": {"VALIDATION": 1}}

        result = emerger._mutate_task_for_completion(
            task_info, ValidationFailedError("не прошло валидацию"), history
        )

        self.assertEqual(result, task_info)

    def test_unconvergent_multichunk_partial_is_dropped_without_becoming_a_chapter(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(total_chunks=3, chunk_index=1, partial=CHAPTER_MIDDLE))
        history = {"total_count": 4, "errors": {"VALIDATION": 4}}

        _, payload = emerger._mutate_task_for_completion(
            task_info, ValidationFailedError("не прошло валидацию"), history
        )

        self.assertEqual(payload, _chunk_payload(total_chunks=3, chunk_index=1))

    def test_truncation_still_accumulates_despite_validation_history(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(partial=CHAPTER_HEAD))
        history = {"total_count": 5, "errors": {"VALIDATION": 5}}

        _, payload = emerger._mutate_task_for_completion(
            task_info, _truncated(CHAPTER_MIDDLE), history
        )

        self.assertEqual(len(payload), 9)
        self.assertTrue(payload[8].startswith(CHAPTER_HEAD))

    def test_real_multichunk_task_is_never_turned_into_a_chapter(self):
        worker = _DummyWorker()
        emerger = EmergencyTask(worker)
        task_info = ("task-id", _chunk_payload(total_chunks=2, chunk_index=1, partial=CHAPTER_HEAD))

        _, payload = emerger._mutate_task_for_completion(task_info, _truncated(DEGENERATE_TAIL))

        self.assertEqual(payload[0], "epub_chunk")
        self.assertEqual(len(payload), 8)


if __name__ == "__main__":
    unittest.main()


class LegacySingleChunkDemotionTests(unittest.TestCase):
    """Старые «ЧАНК 1/1» из снапшота очереди возвращаются к обычной главе."""

    def setUp(self):
        from types import SimpleNamespace
        from gemini_translator.core.task_manager import ChapterQueueManager

        # QObject нельзя создать через __new__, а методу нужен только атрибут
        # класса — поэтому зовём его как обычную функцию с лёгкой заглушкой.
        self.demote = ChapterQueueManager._demote_legacy_single_chunk
        self.manager = SimpleNamespace(
            _DEMOTABLE_SINGLE_CHUNK_STATUSES=ChapterQueueManager._DEMOTABLE_SINGLE_CHUNK_STATUSES,
        )

    def test_pending_single_chunk_becomes_a_chapter(self):
        payload = _chunk_payload(partial=CHAPTER_MIDDLE)

        self.assertEqual(
            self.demote(self.manager, payload, 'pending'),
            ('epub', 'book.epub', 'Text/ch.xhtml'),
        )

    def test_failed_single_chunk_becomes_a_chapter(self):
        self.assertEqual(
            self.demote(self.manager, _chunk_payload(), 'failed'),
            ('epub', 'book.epub', 'Text/ch.xhtml'),
        )

    def test_completed_single_chunk_is_left_for_the_assembler(self):
        payload = _chunk_payload()

        self.assertEqual(self.demote(self.manager, payload, 'completed'), payload)

    def test_real_multichunk_task_is_untouched(self):
        payload = _chunk_payload(total_chunks=2, chunk_index=1)

        self.assertEqual(self.demote(self.manager, payload, 'pending'), payload)

    def test_plain_chapter_is_untouched(self):
        payload = ('epub', 'book.epub', 'Text/ch.xhtml')

        self.assertEqual(self.demote(self.manager, payload, 'pending'), payload)
