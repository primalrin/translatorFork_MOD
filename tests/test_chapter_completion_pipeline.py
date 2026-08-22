# -*- coding: utf-8 -*-
"""
До-перевод оборванной главы больше не требует превращать её в «ЧАНК 1/1».

Хвост лежит в payload[3] обычной задачи 'epub', EpubSingleFileProcessor
подставляет его в промпт до-генерации, склеивает с продолжением и отдаёт
наружу payload уже без хвоста.
"""

import asyncio
import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from gemini_translator.core.worker_helpers.taskers.epub_single_file_processor import (
    EpubSingleFileProcessor,
)


CHAPTER_HTML = (
    "<html><body>"
    "<p>First paragraph of the chapter, long enough to look like real prose.</p>"
    "<p>Second paragraph of the chapter, also long enough to look real.</p>"
    "<p>Third paragraph of the chapter, closing the scene for good.</p>"
    "</body></html>"
)
PARTIAL = "<p>Первый абзац главы, достаточно длинный, чтобы выглядеть живой прозой.</p>"
CONTINUATION = (
    "<p>Второй абзац главы, тоже достаточно длинный, чтобы выглядеть живым.</p>"
    "<p>Третий абзац главы, закрывающий сцену окончательно.</p>"
)


class _ResponseParser:
    def __init__(self):
        self.saved_body = None

    def _restore_media_from_placeholders(self, translated_content, original_content_for_map_building):
        return translated_content

    def process_and_save_single_file(self, translated_body_content, **kwargs):
        self.saved_body = translated_body_content


class _PromptBuilder:
    def __init__(self):
        self.last_completion_data = None
        self.last_text_content = None

    def _replace_media_with_placeholders(self, text):
        return text

    def prepare_for_api(self, text_content, system_instruction_text,
                        completion_data=None, current_chapters_list=None,
                        previous_chapter_reference=None):
        self.last_text_content = text_content
        self.last_completion_data = completion_data
        return "prompt", system_instruction_text, {}


def _make_processor(tmpdir, epub_path):
    parser = _ResponseParser()
    builder = _PromptBuilder()
    worker = SimpleNamespace(
        api_key="key-1234",
        is_cancelled=False,
        output_folder=tmpdir,
        provider_config={"file_suffix": "_translated.html"},
        system_instruction="translate",
        force_accept=False,
        project_manager=None,
        task_manager=None,
        prompt_builder=builder,
        response_parser=parser,
        context_manager=SimpleNamespace(prepare_html_for_translation=lambda text: text),
        _post_event=lambda name, data=None: None,
    )
    return EpubSingleFileProcessor(worker), builder, parser


class ChapterCompletionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.epub_path = os.path.join(self.tmpdir, "book.epub")
        with zipfile.ZipFile(self.epub_path, "w") as epub_zip:
            epub_zip.writestr("Text/ch1.xhtml", CHAPTER_HTML)

    def _run(self, task_payload, api_response):
        processor, builder, parser = _make_processor(self.tmpdir, self.epub_path)
        with patch.object(EpubSingleFileProcessor, "_should_use_json_epub_pipeline", return_value=False), \
             patch.object(EpubSingleFileProcessor, "_execute_api_call",
                          new=lambda self, *a, **kw: _immediate(api_response)):
            result = asyncio.run(processor.execute(("task-id", task_payload), use_stream=False))
        return result, builder, parser

    def test_stored_partial_goes_into_the_completion_prompt(self):
        payload = ("epub", self.epub_path, "Text/ch1.xhtml", PARTIAL)

        _, builder, _ = self._run(payload, CONTINUATION)

        self.assertIsNotNone(builder.last_completion_data)
        self.assertIn("Первый абзац", builder.last_completion_data["partial_translation"])

    def test_result_merges_partial_with_the_continuation(self):
        payload = ("epub", self.epub_path, "Text/ch1.xhtml", PARTIAL)

        _, _, parser = self._run(payload, CONTINUATION)

        self.assertIn("Первый абзац", parser.saved_body)
        self.assertIn("Третий абзац", parser.saved_body)
        self.assertEqual(parser.saved_body.count("Первый абзац"), 1)

    def test_success_returns_payload_without_the_partial(self):
        payload = ("epub", self.epub_path, "Text/ch1.xhtml", PARTIAL)

        (result_task_info, _), success, status, _ = self._run(payload, CONTINUATION)[0]

        self.assertTrue(success)
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(result_task_info[1], ("epub", self.epub_path, "Text/ch1.xhtml"))

    def test_plain_chapter_without_partial_uses_no_completion_prompt(self):
        payload = ("epub", self.epub_path, "Text/ch1.xhtml")
        whole = PARTIAL + CONTINUATION

        _, builder, parser = self._run(payload, whole)

        self.assertIsNone(builder.last_completion_data)
        self.assertIn("Третий абзац", parser.saved_body)


async def _immediate(value):
    return value


if __name__ == "__main__":
    unittest.main()
