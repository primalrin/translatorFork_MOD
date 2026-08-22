# -*- coding: utf-8 -*-

import os

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import ValidationFailedError, PartialGenerationError
from gemini_translator.utils.epub_json import (
    build_html_document_model,
    estimate_translation_noise,
)
from gemini_translator.utils.epub_tools import normalize_epub_chapter_heading_to_h1
from gemini_translator.utils.text import (
    is_content_effectively_empty,
    clean_html_content,
    merge_partial_with_overlap_guard,
    validate_html_structure,
    coerce_translated_body_block,
)


class EpubChunkProcessor(BaseTaskProcessor):
    def _merge_with_overlap_guard(self, partial_text: str, new_text: str):
        return merge_partial_with_overlap_guard(partial_text, new_text)

    def _normalize_body_wrapper(self, original_html: str, translated_html: str):
        return coerce_translated_body_block(original_html, translated_html)

    def _build_sequential_reference(self, task_info, chapter_path, chunk_index, total_chunks):
        prompt_builder = getattr(self.worker, "prompt_builder", None)
        if not prompt_builder or not getattr(prompt_builder, "sequential_mode", False):
            return None

        try:
            chunk_index = int(chunk_index)
        except (TypeError, ValueError):
            return prompt_builder._build_previous_chapter_reference([chapter_path])

        if chunk_index <= 0:
            return prompt_builder._build_previous_chapter_reference([chapter_path])

        previous_translation = ""
        task_manager = getattr(self.worker, "task_manager", None)
        if task_manager and hasattr(task_manager, "get_completed_previous_chunk_translation"):
            try:
                previous_translation = task_manager.get_completed_previous_chunk_translation(
                    current_task_id=task_info[0] if task_info else None,
                    chapter_path=chapter_path,
                    chunk_index=chunk_index,
                )
            except Exception:
                previous_translation = ""

        previous_chunk_number = chunk_index
        if not previous_translation:
            return (
                "PREVIOUS TRANSLATED CHUNK NOT AVAILABLE YET: "
                f"{chapter_path} [{previous_chunk_number}/{total_chunks}]"
            )

        if hasattr(prompt_builder, "_truncate_sequential_reference"):
            previous_translation = prompt_builder._truncate_sequential_reference(
                previous_translation,
                "previous chunk reference",
            )

        return (
            f"Previous translated chunk: {chapter_path} "
            f"[{previous_chunk_number}/{total_chunks}]\n\n{previous_translation}"
        )

    async def _execute_json_chunk_pipeline(self, task_info, chapter_path, content_to_translate_for_api, log_prefix, use_stream, chunk_index, total_chunks, is_retry, previous_reference=None):
        document_model = build_html_document_model(content_to_translate_for_api, document_id=chapter_path)
        user_prompt, _, _, source_payload = self.worker.prompt_builder.prepare_json_for_api(
            document_model=document_model,
            raw_source_text=content_to_translate_for_api,
            system_instruction_text=self.worker.system_instruction,
            current_chapters_list=[chapter_path],
            previous_chapter_reference=previous_reference,
        )
        operation_context = self._build_operation_context(
            task_info,
            action='translate_chunk_json',
            chapter=chapter_path,
            chapters=[chapter_path],
            task_type='epub_chunk',
            chunk_index=chunk_index + 1,
            chunk_total=total_chunks,
            is_retry=is_retry,
        )
        raw_response = await self._execute_api_call(
            prompt=user_prompt,
            log_prefix=f"{log_prefix} [JSON]",
            task_info=task_info,
            operation_context=operation_context,
            use_stream=use_stream,
        )
        translated_full_html = self.worker.response_parser.parse_json_translation_response(
            raw_response=raw_response,
            document_model=document_model,
            source_payload=source_payload,
        )
        translated_full_html = self._normalize_body_wrapper(content_to_translate_for_api, translated_full_html)
        return raw_response, translated_full_html, source_payload

    async def execute(self, task_info, use_stream=True):
        task_id, task_payload = task_info

        is_retry = len(task_payload) > 8
        base_payload = task_payload[:-1] if is_retry else task_payload
        partial_translation = task_payload[-1] if is_retry else None

        _, epub_path, chapter_path, chunk_content, chunk_index, total_chunks, prefix, suffix = base_payload
        log_prefix = f"{os.path.basename(chapter_path)} [{chunk_index + 1}/{total_chunks}]" + (" [Попытка 2+]" if is_retry else "")

        content_to_translate_for_api = chunk_content
        if not chunk_content.lower().strip().startswith('<body'):
            content_to_translate_for_api = "<body>" + content_to_translate_for_api
        if not chunk_content.lower().strip().endswith('</body>'):
            content_to_translate_for_api = content_to_translate_for_api + "</body>"
        content_to_translate_for_api = normalize_epub_chapter_heading_to_h1(content_to_translate_for_api)

        segmented_text = self.worker.context_manager.prepare_html_for_translation(content_to_translate_for_api)
        content_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(segmented_text)

        if is_content_effectively_empty(content_with_placeholders):
            final_restored_html = content_to_translate_for_api
            result_payload = ((task_id, tuple(base_payload)), final_restored_html)
            return result_payload, True, 'SUCCESS', "Пропущено (нет текста, только медиа/теги)"

        previous_reference = self._build_sequential_reference(
            task_info=task_info,
            chapter_path=chapter_path,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )

        use_json_pipeline = self._should_use_json_epub_pipeline() and not partial_translation
        if use_json_pipeline:
            try:
                raw_response, final_restored_html, source_payload = await self._execute_json_chunk_pipeline(
                    task_info=task_info,
                    chapter_path=chapter_path,
                    content_to_translate_for_api=content_to_translate_for_api,
                    log_prefix=log_prefix,
                    use_stream=use_stream,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    is_retry=is_retry,
                    previous_reference=previous_reference,
                )
                if not getattr(self.worker, "force_accept", False):
                    original_chunk_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(content_to_translate_for_api)
                    is_valid, reason, validated_chunk = validate_html_structure(original_chunk_with_placeholders, final_restored_html)
                    if not is_valid:
                        self._raise_validation_error(
                            f"JSON chunk не прошел финальную HTML-валидацию: {reason}",
                            raw_response
                        )
                    final_restored_html = validated_chunk

                noise_report = estimate_translation_noise(content_to_translate_for_api, source_payload)
                self.worker._post_event('log_message', {
                    'message': (
                        f"[JSON EPUB CHUNK] '{os.path.basename(chapter_path)}' "
                        f"[{chunk_index + 1}/{total_chunks}]: "
                        f"HTML noise={noise_report['html_markup_chars']}, "
                        f"JSON overhead={noise_report['json_overhead_chars']}."
                    )
                })
                result_payload = (
                    (task_id, tuple(base_payload)),
                    self._build_success_payload(
                        translated_content=final_restored_html,
                        details_text=raw_response,
                        details_title=f"JSON-пакет для '{os.path.basename(chapter_path)}' [{chunk_index + 1}/{total_chunks}]",
                        preview_limit=0,
                    )
                )
                return result_payload, True, 'SUCCESS', "Успешно"
            except (ValidationFailedError, PartialGenerationError, ValueError) as json_error:
                self.worker._post_event('log_message', {
                    'message': f"[JSON EPUB CHUNK] Откат на legacy HTML для '{os.path.basename(chapter_path)}' [{chunk_index + 1}/{total_chunks}]: {json_error}"
                })

        completion_data = None
        if partial_translation:
            clean_partial_for_prompt = clean_html_content(partial_translation, is_html=False)
            completion_data = {
                'original_content': content_to_translate_for_api,
                'partial_translation': clean_partial_for_prompt
            }

        user_prompt, _, _ = self.worker.prompt_builder.prepare_for_api(
            text_content=content_to_translate_for_api,
            system_instruction_text=self.worker.system_instruction,
            completion_data=completion_data,
            current_chapters_list=[chapter_path],
            previous_chapter_reference=previous_reference,
        )

        newly_generated_part_raw = ""
        try:
            operation_context = self._build_operation_context(
                task_info,
                action='translate_chunk',
                chapter=chapter_path,
                chapters=[chapter_path],
                task_type='epub_chunk',
                chunk_index=chunk_index + 1,
                chunk_total=total_chunks,
                is_retry=is_retry,
            )
            newly_generated_part_raw = await self._execute_api_call(
                user_prompt,
                log_prefix,
                task_info=task_info,
                operation_context=operation_context,
                use_stream=use_stream,
            )
        except PartialGenerationError as e:
            if e.partial_text:
                e.partial_text = clean_html_content(e.partial_text, is_html=False)
            raise e

        cleaned_new_part_from_markdown = clean_html_content(newly_generated_part_raw, is_html=False)

        cleaned_partial_from_markdown = ""
        if partial_translation:
            cleaned_partial_from_markdown = clean_html_content(partial_translation, is_html=False)

        accumulated_raw_html, overlap_len = self._merge_with_overlap_guard(
            cleaned_partial_from_markdown,
            cleaned_new_part_from_markdown
        )
        accumulated_text = clean_html_content(accumulated_raw_html, is_html=True)
        accumulated_text = self._normalize_body_wrapper(content_to_translate_for_api, accumulated_text)

        original_chunk_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(content_to_translate_for_api)

        if not getattr(self.worker, "force_accept", False):
            is_valid, reason, validated_chunk = validate_html_structure(original_chunk_with_placeholders, accumulated_text)
            if not is_valid:
                self._raise_validation_error(
                    f"Финальный текст не прошел валидацию: {reason}",
                    accumulated_raw_html or newly_generated_part_raw
                )
            accumulated_text = validated_chunk

        final_restored_html = self.worker.response_parser._restore_media_from_placeholders(
            translated_content=accumulated_text,
            original_content_for_map_building=content_to_translate_for_api
        )

        base_payload = task_payload[:-1] if len(task_payload) > 8 else task_payload
        result_payload = (
            (task_id, tuple(base_payload)),
            self._build_success_payload(
                translated_content=final_restored_html,
                details_text=accumulated_raw_html or newly_generated_part_raw,
                details_title=(
                    f"Полученный пакет для '{os.path.basename(chapter_path)}' [{chunk_index + 1}/{total_chunks}]"
                    + (f" [anti-dup overlap: {overlap_len}]" if overlap_len else "")
                ),
                preview_limit=0,
            )
        )
        return result_payload, True, 'SUCCESS', "Успешно"
