# -*- coding: utf-8 -*-

import os
import zipfile

from .base_processor import BaseTaskProcessor
from gemini_translator.api.errors import ValidationFailedError, PartialGenerationError
from gemini_translator.utils.epub_json import (
    build_html_document_model,
    estimate_translation_noise,
)
from gemini_translator.utils.epub_tools import normalize_epub_chapter_heading_to_h1
from gemini_translator.utils.translated_paths import build_translated_output_path
from gemini_translator.utils.text import (
    process_body_tag, is_content_effectively_empty, clean_html_content,
    merge_partial_with_overlap_guard, validate_html_structure
)


class EpubSingleFileProcessor(BaseTaskProcessor):
    @staticmethod
    def extract_partial_translation(task_payload):
        """
        Хвост оборванного перевода живёт в payload[3] обычной задачи 'epub'.

        Раньше ради до-генерации главу переодевали в 'epub_chunk' 0/1 — и она
        так и оставалась в списке «ЧАНК 1/1». Теперь глава остаётся главой.
        """
        if len(task_payload) <= 3:
            return ""
        partial = task_payload[3]
        return partial if isinstance(partial, str) and partial.strip() else ""

    async def _execute_json_pipeline(self, task_info, body_content, original_content, internal_chapter_path, log_prefix, use_stream):
        document_model = build_html_document_model(original_content, document_id=internal_chapter_path)
        user_prompt, _, _, source_payload = self.worker.prompt_builder.prepare_json_for_api(
            document_model=document_model,
            raw_source_text=body_content,
            system_instruction_text=self.worker.system_instruction,
            current_chapters_list=[internal_chapter_path]
        )
        operation_context = self._build_operation_context(
            task_info,
            action='translate_chapter_json',
            chapter=internal_chapter_path,
            chapters=[internal_chapter_path],
            task_type='epub',
        )
        raw_response = await self._execute_api_call(
            user_prompt,
            f"{log_prefix} [JSON]",
            task_info=task_info,
            operation_context=operation_context,
            use_stream=use_stream,
        )
        translated_full_html = self.worker.response_parser.parse_json_translation_response(
            raw_response=raw_response,
            document_model=document_model,
            source_payload=source_payload,
        )
        return raw_response, translated_full_html, source_payload

    async def execute(self, task_info, use_stream=True):
        """Самодостаточно обрабатывает ОДНУ задачу типа 'epub'."""
        task_id, task_payload = task_info

        try:
            epub_path = task_payload[1]
            internal_chapter_path = task_payload[2]
        except IndexError:
            raise ValueError(f"Некорректный формат задачи epub: {task_payload}")

        partial_translation = self.extract_partial_translation(task_payload)
        # Хвост не должен пережить задачу: наружу отдаём payload без него.
        base_task_info = (task_id, tuple(task_payload[:3]))

        log_prefix = f"{os.path.basename(internal_chapter_path)} (ключ …{self.worker.api_key[-4:]})"
        if partial_translation:
            log_prefix += " [Доперевод]"

        if self.worker.is_cancelled:
            return task_info, False, 'CANCELLED', "Отменено пользователем"

        with zipfile.ZipFile(epub_path, "r") as zf:
            original_content = zf.read(internal_chapter_path).decode("utf-8", "ignore")
        original_content = normalize_epub_chapter_heading_to_h1(original_content)

        prefix_html, body_content, html_suffix = process_body_tag(original_content, return_parts=True, body_content_only=False)

        version_suffix = self.worker.provider_config['file_suffix']
        out_path = build_translated_output_path(
            self.worker.output_folder,
            internal_chapter_path,
            version_suffix,
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        if not body_content.strip():
            self._copy_original_as_result(out_path, original_content, internal_chapter_path, version_suffix)
            return base_task_info, True, 'SUCCESS', "Файл пуст или с пустым <body>, скопирован."

        segmented_text = self.worker.context_manager.prepare_html_for_translation(body_content)
        content_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(segmented_text)

        if is_content_effectively_empty(content_with_placeholders):
            self._copy_original_as_result(out_path, original_content, internal_chapter_path, version_suffix)
            return base_task_info, True, 'SUCCESS', "Пропущено (нет текста, только медиа/теги), оригинал скопирован."

        use_json_pipeline = self._should_use_json_epub_pipeline() and not partial_translation
        if use_json_pipeline:
            try:
                raw_response, translated_full_html, source_payload = await self._execute_json_pipeline(
                    task_info=task_info,
                    body_content=body_content,
                    original_content=original_content,
                    internal_chapter_path=internal_chapter_path,
                    log_prefix=log_prefix,
                    use_stream=use_stream,
                )
                noise_report = estimate_translation_noise(body_content, source_payload)
                translated_body = process_body_tag(
                    translated_full_html,
                    return_parts=False,
                    body_content_only=False
                )
                self.worker.response_parser.process_and_save_single_file(
                    translated_body_content=translated_body,
                    original_full_content=original_content,
                    prefix_html=prefix_html,
                    suffix_html=html_suffix,
                    output_path=out_path,
                    original_internal_path=internal_chapter_path,
                    version_suffix=version_suffix
                )
                self.worker._post_event('log_message', {
                    'message': (
                        f"[JSON EPUB] '{os.path.basename(internal_chapter_path)}': "
                        f"HTML noise={noise_report['html_markup_chars']}, "
                        f"JSON overhead={noise_report['json_overhead_chars']}."
                    )
                })
                success_payload = self._build_success_payload(
                    details_text=raw_response,
                    details_title=f"JSON-пакет для '{os.path.basename(internal_chapter_path)}'",
                    preview_limit=4000,
                )
                return (base_task_info, success_payload), True, 'SUCCESS', ""
            except (ValidationFailedError, PartialGenerationError, ValueError) as json_error:
                self.worker._post_event('log_message', {
                    'message': f"[JSON EPUB] Откат на legacy HTML для '{os.path.basename(internal_chapter_path)}': {json_error}"
                })

        completion_data = None
        if partial_translation:
            completion_data = {
                'original_content': body_content,
                'partial_translation': clean_html_content(partial_translation, is_html=False),
            }

        user_prompt, _, _ = self.worker.prompt_builder.prepare_for_api(
            body_content,
            self.worker.system_instruction,
            completion_data=completion_data,
            current_chapters_list=[internal_chapter_path]
        )

        original_was_a_body = (
            body_content.strip().lower().startswith('<body')
            and body_content.strip().lower().endswith('</body>')
        )

        raw_response = ""
        try:
            operation_context = self._build_operation_context(
                task_info,
                action='translate_chapter',
                chapter=internal_chapter_path,
                chapters=[internal_chapter_path],
                task_type='epub',
            )
            raw_response = await self._execute_api_call(
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

        if partial_translation:
            accumulated_raw_html, overlap_len = merge_partial_with_overlap_guard(
                clean_html_content(partial_translation, is_html=False),
                clean_html_content(raw_response, is_html=False),
            )
            cleaned_response = clean_html_content(accumulated_raw_html, is_html=original_was_a_body)
            self.worker._post_event('log_message', {'message': (
                f"[INFO] Доперевод главы '{os.path.basename(internal_chapter_path)}' склеен"
                + (f" (перекрытие {overlap_len} симв. срезано)." if overlap_len else ".")
            )})
        else:
            cleaned_response = clean_html_content(raw_response, is_html=original_was_a_body)

        original_body_with_placeholders = self.worker.prompt_builder._replace_media_with_placeholders(body_content)

        if not getattr(self.worker, "force_accept", False):
            is_valid, reason, validated_html = validate_html_structure(original_body_with_placeholders, cleaned_response)
            if not is_valid:
                self._raise_validation_error(
                    f"Ответ не прошел валидацию: {reason}",
                    raw_response or cleaned_response
                )
            cleaned_response = validated_html

        restored_body = self.worker.response_parser._restore_media_from_placeholders(
            translated_content=cleaned_response,
            original_content_for_map_building=original_content
        )

        if not restored_body or not restored_body.strip():
            self._raise_validation_error(
                "API вернуло пустой ответ после очистки и восстановления.",
                raw_response or cleaned_response
            )

        self.worker.response_parser.process_and_save_single_file(
            translated_body_content=restored_body,
            original_full_content=original_content,
            prefix_html=prefix_html,
            suffix_html=html_suffix,
            output_path=out_path,
            original_internal_path=internal_chapter_path,
            version_suffix=version_suffix
        )

        success_payload = self._build_success_payload(
            details_text=raw_response or cleaned_response,
            details_title=f"Полученный пакет для '{os.path.basename(internal_chapter_path)}'",
            preview_limit=4000,
        )
        return (base_task_info, success_payload), True, 'SUCCESS', ""

    def _copy_original_as_result(self, out_path, content, internal_path, suffix):
        """Копирует оригинал на диск и регистрирует его в проекте."""
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        if self.project_manager:
            relative_path = os.path.relpath(out_path, self.project_manager.project_folder)
            self.project_manager.register_translation(
                original_internal_path=internal_path,
                version_suffix=suffix,
                translated_relative_path=relative_path
            )
