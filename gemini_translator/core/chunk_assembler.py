import os
import threading
import zipfile
import re
import json
from collections import defaultdict, Counter
from PyQt6 import QtWidgets
from PyQt6 import QtCore
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from ..api import config as api_config
from ..utils.translated_paths import build_translated_output_path
from ..utils.text import prettify_html, process_body_tag
class ChunkAssembler(QObject):
    """
    Отслеживает и собирает переведенные чанки в финальные файлы глав.
    Класс является потокобезопасным.
    """
    
    def __init__(self, output_folder, project_manager=None, settings=None):
        super().__init__()
        self.bus = QtWidgets.QApplication.instance().event_bus
        self._event_topics = (
            'session_started',
            'chunk_task_completed',
            'task_state_changed',
        )
        if hasattr(self.bus, "subscribe"):
            for topic in self._event_topics:
                self.bus.subscribe(topic, self.on_event)
        else:
            self.bus.event_posted.connect(self.on_event)
        
        self.settings = settings or {}
        
        self.output_folder = output_folder
        self.project_manager = project_manager
        
        # self.assembly_bay и self.lock больше не нужны
        self.session_id = None
        self._pending_chapter_paths = set()
        self._recovery_scan_done = False
        self._assembly_timer = QtCore.QTimer(self)
        self._assembly_timer.setSingleShot(True)
        self._assembly_timer.setInterval(350)
        self._assembly_timer.timeout.connect(self._run_scheduled_assembly_check)


    @pyqtSlot(dict)
    def on_event(self, event: dict):
        """Принимает события из общей шины."""
        event_name = event.get('event')
        if self.session_id is None and event.get('session_id'):
            self.session_id = event.get('session_id')

        if event_name == 'session_started':
            self._recovery_scan_done = False
            self._schedule_assembly_scan(full_scan=True)
            return

        if event_name == 'chunk_task_completed':
            chapter_path = event.get('data', {}).get('chapter_path')
            if chapter_path:
                self._schedule_assembly_scan(chapter_path=chapter_path)
            return

        if event_name == 'task_state_changed' and not self._recovery_scan_done:
            full_state = event.get('data', {}).get('full_state')
            if isinstance(full_state, list):
                for item in full_state:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    task_info, status = item[0], item[1]
                    payload = task_info[1] if isinstance(task_info, tuple) and len(task_info) > 1 else ()
                    if status == 'success' and payload and payload[0] == 'epub_chunk':
                        self._schedule_assembly_scan(full_scan=True)
                        self._recovery_scan_done = True
                        break

    def _schedule_assembly_scan(self, chapter_path: str | None = None, full_scan: bool = False):
        if full_scan:
            self._pending_chapter_paths.clear()
            self._pending_chapter_paths.add('*')
        elif chapter_path:
            if '*' not in self._pending_chapter_paths:
                self._pending_chapter_paths.add(str(chapter_path))
        self._assembly_timer.start()

    def _run_scheduled_assembly_check(self):
        if '*' in self._pending_chapter_paths:
            self._pending_chapter_paths.clear()
            self.run_final_assembly_check()
            return

        chapter_paths = list(self._pending_chapter_paths)
        self._pending_chapter_paths.clear()
        self.run_final_assembly_check(chapter_paths=chapter_paths)

    def _post_event(self, name: str, data: dict = None):
        event = {
            'event': name, 'source': 'ChunkAssembler', 
            'session_id': self.session_id, 'data': data or {}
        }
        if hasattr(self.bus, "emit_event"):
            self.bus.emit_event(event)
        else:
            self.bus.event_posted.emit(event)

    def _build_wrapper_from_source(self, epub_path: str, original_chapter_path: str):
        with open(epub_path, "rb") as epub_file, zipfile.ZipFile(epub_file, "r") as epub_zip:
            original_html = epub_zip.read(original_chapter_path).decode("utf-8", "ignore")
        prefix, _, suffix = process_body_tag(original_html, return_parts=True, body_content_only=True)
        return prefix, suffix

    def _source_candidates_from_virtual_path(self, epub_path: str):
        if not isinstance(epub_path, str) or not epub_path.startswith("mem://"):
            return []

        internal_path = epub_path[len("mem://"):].lstrip("/")
        if not internal_path:
            return []

        candidates = []
        drive_match = re.match(r"^([A-Za-z])_drive(?:/|$)(.*)$", internal_path)
        if drive_match:
            drive, tail = drive_match.groups()
            drive_path = f"{drive}:/{tail}" if tail else f"{drive}:/"
            candidates.append(drive_path.replace("/", os.sep))

        candidates.append("/" + internal_path)
        candidates.append(internal_path.replace("/", os.sep))
        return candidates

    def _add_epub_candidate(self, candidates: list, seen: set, epub_path):
        if not epub_path:
            return
        epub_path = str(epub_path)
        for candidate in (epub_path, *self._source_candidates_from_virtual_path(epub_path)):
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    def _resolve_wrapper(self, first_payload: list, original_chapter_path: str):
        if len(first_payload) >= 8:
            prefix = first_payload[6]
            suffix = first_payload[7]
            if isinstance(prefix, str) and isinstance(suffix, str):
                return prefix, suffix

        epub_candidates = []
        seen_candidates = set()
        if len(first_payload) > 1:
            self._add_epub_candidate(epub_candidates, seen_candidates, first_payload[1])
        for settings_key in ('file_path', 'source_file_path', 'original_epub_path'):
            self._add_epub_candidate(epub_candidates, seen_candidates, self.settings.get(settings_key))

        last_error = None
        for epub_path in epub_candidates:
            try:
                return self._build_wrapper_from_source(epub_path, original_chapter_path)
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        raise RuntimeError(f"Не удалось определить HTML-обертку для '{original_chapter_path}'.")

    def _requeue_chunks_missing_results(self, conn, task_ids: list, result_ids: set, original_chapter_path: str) -> bool:
        missing_ids = [task_id for task_id in task_ids if task_id not in result_ids]
        if not missing_ids:
            return False

        placeholders = ','.join('?' for _ in missing_ids)
        cursor = conn.execute(
            f"SELECT task_id FROM tasks WHERE task_id IN ({placeholders}) AND status = 'completed'",
            missing_ids,
        )
        recoverable_ids = [row['task_id'] for row in cursor.fetchall()]
        if not recoverable_ids:
            return False

        recoverable_placeholders = ','.join('?' for _ in recoverable_ids)
        conn.execute(
            f"""
            UPDATE tasks
            SET status = 'pending',
                priority = 1,
                worker_id = NULL
            WHERE task_id IN ({recoverable_placeholders})
            """,
            recoverable_ids,
        )
        self._post_event('log_message', {
            'message': (
                f"[ASSEMBLER_RECOVERY] Для '{os.path.basename(original_chapter_path)}' "
                f"не найдено результатов чанков: {len(recoverable_ids)}. "
                "Они возвращены в очередь для повторного перевода."
            )
        })
        return True

    def _assemble_chapter_from_db(self, task_ids: list, original_chapter_path: str):
        """
        Извлекает результаты чанков из БД и собирает главу.
        Результаты удаляются только после успешной записи итогового файла.
        """
        app = QtWidgets.QApplication.instance()
        if not hasattr(app, 'task_manager'): return

        try:
            placeholders = ','.join('?' for _ in task_ids)
            
            # --- НАЧАЛО ЕДИНОЙ АТОМАРНОЙ ОПЕРАЦИИ ---
            with app.task_manager._get_write_conn() as conn:
                # 1. Проверяем наличие всех результатов
                cursor = conn.execute(f"SELECT task_id, translated_content, provider_id FROM chunk_results WHERE task_id IN ({placeholders})", task_ids)
                results = cursor.fetchall()

                if len(results) != len(task_ids):
                    result_ids = {row['task_id'] for row in results}
                    recovered = self._requeue_chunks_missing_results(
                        conn,
                        task_ids,
                        result_ids,
                        original_chapter_path,
                    )
                    if recovered:
                        app.task_manager._safe_request_ui_update()
                    else:
                        # Если результатов не хватает, ничего не делаем. Транзакция просто завершится.
                        print(f"[ASSEMBLER_RACE_CONDITION] Сборка для '{os.path.basename(original_chapter_path)}' отменена: другой поток уже забрал эти чанки.")
                    return

                # 2. Получаем все необходимые payload'ы в этой же транзакции.
                # Результаты чанков удаляются только после успешной записи финального файла.
                cursor = conn.execute(f"SELECT task_id, payload FROM tasks WHERE task_id IN ({placeholders})", task_ids)
                chunk_infos_rows = cursor.fetchall()
                if not chunk_infos_rows:
                     raise RuntimeError(f"Не удалось найти payload'ы для чанков главы {original_chapter_path}")
            # --- КОНЕЦ ЕДИНОЙ АТОМАРНОЙ ОПЕРАЦИИ. conn.commit() вызван автоматически ---

            # --- Теперь мы эксклюзивно владеем данными и можем спокойно работать вне транзакции ---
            results_map = {row['task_id']: row['translated_content'] for row in results}
            chunk_infos = [{'task_id': row['task_id'], 'payload': json.loads(row['payload'])} for row in chunk_infos_rows]
            
            first_payload = chunk_infos[0]['payload']
            epub_path, total_chunks = first_payload[1], first_payload[5]
            prefix, suffix = self._resolve_wrapper(first_payload, original_chapter_path)
            
            self._post_event('log_message', {'message': f"[ASSEMBLER] Комплект из {total_chunks} чанков для '{os.path.basename(original_chapter_path)}' захвачен для сборки…"})

            provider_id = Counter(row['provider_id'] for row in results).most_common(1)[0][0] if results else 'gemini'
            
            sorted_chunks = [process_body_tag(results_map[info['task_id']], return_parts=False, body_content_only=True) for info in sorted(chunk_infos, key=lambda x: x['payload'][4])]
            
            full_content = "".join(sorted_chunks)
            final_html = prefix + full_content + suffix
            if self.settings:
                if self.settings.get("use_prettify", False):
                    final_html = prettify_html(final_html)
            else:
                final_html = prettify_html(final_html)
            
            provider_config = api_config.api_providers().get(provider_id, {})
            file_suffix = provider_config.get('file_suffix', '_translated.html')

            final_path = build_translated_output_path(
                self.output_folder,
                original_chapter_path,
                file_suffix,
            )
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            with open(final_path, "w", encoding="utf-8") as f: f.write(final_html)

            if self.project_manager:
                relative_path = os.path.relpath(final_path, self.output_folder)
                self.project_manager.register_translation(original_chapter_path, file_suffix, relative_path)

            replaced = app.task_manager.replace_chunks_with_chapter(
                chunk_task_ids=task_ids, epub_path=epub_path, original_chapter_path=original_chapter_path
            )
            if not replaced:
                return
            
            self._post_event('log_message', {'message': f"[ASSEMBLER] ✅ Глава '{os.path.basename(original_chapter_path)}' успешно собрана из комплекта."})
            self._post_event('assembly_finished', {'original_chapter_path': original_chapter_path, 'chunk_count': total_chunks})

        except Exception as e:
            self._post_event('log_message', {'message': f"[ASSEMBLER_ERROR] КРИТИЧЕСКАЯ ОШИБКА при сборке главы '{os.path.basename(original_chapter_path)}': {e}"})

    def run_final_assembly_check(self, chapter_paths: list[str] | None = None):
        """
        Находит в БД все успешные чанки и запускает сборку для КАЖДОГО
        найденного полного комплекта.
        """
        if not self.project_manager: return
        app = QtWidgets.QApplication.instance()
        if not hasattr(app, 'task_manager'): return

        with app.task_manager._get_read_only_conn() as conn: # Используем 'with conn' для автоматических транзакций при чтении
            query = "SELECT task_id, payload FROM tasks WHERE status = 'completed' AND payload LIKE '%\"epub_chunk\"%'"
            params = []
            filtered_chapter_paths = [str(path) for path in (chapter_paths or []) if path]
            if filtered_chapter_paths:
                like_clause = " OR ".join("payload LIKE ?" for _ in filtered_chapter_paths)
                query += f" AND ({like_clause})"
                params = [f'%"{path}"%' for path in filtered_chapter_paths]
            cursor = conn.execute(query, params)
            completed_chunks = cursor.fetchall()
        
        if not completed_chunks: return

        chunks_by_chapter_and_index = defaultdict(lambda: defaultdict(list))
        for row in completed_chunks:
            try:
                payload = json.loads(row['payload'])
                if payload[0] == 'epub_chunk' and len(payload) >= 6:
                    if filtered_chapter_paths and payload[2] not in filtered_chapter_paths:
                        continue
                    chunks_by_chapter_and_index[payload[2]][payload[4]].append(
                        {'task_id': row['task_id'], 'payload': payload}
                    )
            except (json.JSONDecodeError, IndexError):
                continue
        
        for chapter_path, grouped_chunks in chunks_by_chapter_and_index.items():
            first_index_group = next(iter(grouped_chunks.values()), [])
            if not first_index_group: continue
            
            total_chunks_needed = first_index_group[0]['payload'][5]
            
            if not all(i in grouped_chunks for i in range(total_chunks_needed)):
                continue

            num_possible_assemblies = min(len(grouped_chunks[i]) for i in range(total_chunks_needed))

            for i in range(num_possible_assemblies):
                complete_set_of_infos = [grouped_chunks[idx][i] for idx in range(total_chunks_needed)]
                task_ids_for_assembly = [info['task_id'] for info in complete_set_of_infos]
                
                # Просто запускаем сборку, передавая ей ID задач и путь.
                # Атомарная операция внутри _assemble_chapter_from_db предотвратит двойную сборку.
                QtCore.QTimer.singleShot(0, lambda ids=task_ids_for_assembly, path=chapter_path: self._assemble_chapter_from_db(ids, path))
