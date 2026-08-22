from gemini_translator.api.errors import (
    ErrorType,
    PartialGenerationError,
    ValidationFailedError,
)
from gemini_translator.api import config as api_config
import zipfile
from collections import Counter
from gemini_translator.utils.epub_tools import normalize_epub_chapter_heading_to_h1
from gemini_translator.utils.text import (
    brute_force_split,
    merge_partial_with_overlap_guard,
    sanitize_partial_translation,
)

class EmergencyTask:

    # Где в payload лежит хвост оборванного перевода.
    _PARTIAL_INDEX_BY_TASK_TYPE = {'epub': 3, 'epub_chunk': 8}

    # Сколько провалов валидации терпим, прежде чем счесть до-генерацию
    # безнадёжной и перевести кусок с нуля. Лимит попыток по VALIDATION — 6,
    # так что после сброса остаётся ещё половина бюджета на чистый перевод.
    _MAX_VALIDATION_FAILURES_WITH_PARTIAL = 2

    def __init__(self, worker):
        self.worker = worker

    def _reset_unconvergent_completion(self, task_info, exc, task_history):
        """
        Аварийный сброс до-генерации, которая не сходится.

        Если хвост в payload начинается не с начала куска (такие задачи остались
        от старых запусков, где накопленный перевод затирался продолжением), то
        склейка «середина + хвост» никогда не сойдётся с целой главой, и задача
        молотит попытки впустую до окончательного провала. После нескольких
        провалов валидации выбрасываем хвост и переводим кусок заново.

        Возвращает новый task_info или None, если сбрасывать нечего.
        """
        # Сбрасываем только по валидации: сеть и лимиты ничего не говорят о том,
        # сходится до-генерация или нет, и терять из-за них накопленное нельзя.
        if not isinstance(exc, ValidationFailedError):
            return None

        task_id, task_payload = task_info
        payload_list = list(task_payload or ())
        partial_index = self._PARTIAL_INDEX_BY_TASK_TYPE.get(payload_list[0] if payload_list else None)
        if partial_index is None or len(payload_list) <= partial_index:
            return None
        if not str(payload_list[partial_index] or "").strip():
            return None

        validation_failures = (task_history or {}).get('errors', {}).get(ErrorType.VALIDATION.name, 0)
        if validation_failures < self._MAX_VALIDATION_FAILURES_WITH_PARTIAL:
            return None

        self.worker._post_event('log_message', {
            'message': (
                f"[WARN] До-генерация не сходится (провалов валидации: {validation_failures}) — "
                "накопленный хвост отброшен, кусок будет переведён заново."
            )
        })
        return (task_id, self._payload_without_partial(task_payload))

    def _payload_without_partial(self, task_payload):
        """
        Сбрасывает накопленный до-перевод.

        Чанк 1/1 — это не настоящий чанк, а целая глава: такие задачи остались
        от старой схемы, где до-генерация переодевала главу в 'epub_chunk'.
        Без хвоста маскарад не нужен — возвращаем задачу к обычному 'epub',
        иначе глава так и висит в списке как «ЧАНК 1/1».
        """
        payload = tuple(task_payload)
        partial_index = self._PARTIAL_INDEX_BY_TASK_TYPE.get(payload[0] if payload else None)
        base_payload = payload[:partial_index] if partial_index is not None else payload
        if base_payload[0] != 'epub_chunk' or len(base_payload) < 6:
            return base_payload

        try:
            total_chunks = int(base_payload[5])
        except (TypeError, ValueError):
            return base_payload

        if total_chunks != 1:
            return base_payload

        self.worker._post_event('log_message', {
            'message': (
                "[INFO] Глава возвращена к обычной задаче целиком "
                "(до-генерация чанка 1/1 отменена)."
            )
        })
        return ('epub', base_payload[1], base_payload[2])

    def _mutate_task_for_completion(self, task_info: tuple, exc, task_history: dict | None = None):
        """
        Проверяет, является ли ошибка PartialGenerationError с непустым хвостом.
        Если да - мутирует payload задачи для догенерации.
        В противном случае - возвращает исходный task_info.
        """
        reset_task_info = self._reset_unconvergent_completion(task_info, exc, task_history)
        if reset_task_info is not None:
            return reset_task_info

        if not isinstance(exc, PartialGenerationError) or not getattr(exc, 'partial_text', ''):
            return task_info

        task_id, task_payload = task_info
        untrimmed_partial_text = exc.partial_text

        # --- УМНАЯ ОБРЕЗКА ХВОСТА ---
        split_markers = ["</p>", "</div>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>", "</li>", "</blockquote>", "<br>", "\n"]
        best_split_pos = -1
        for marker in split_markers:
            pos = untrimmed_partial_text.rfind(marker)
            if pos > best_split_pos:
                best_split_pos = pos + len(marker)
        
        partial_text = untrimmed_partial_text[:best_split_pos].rstrip() if best_split_pos != -1 else untrimmed_partial_text
        if partial_text != untrimmed_partial_text and list(task_payload)[0] == 'epub':
            self.worker._post_event('log_message', {'message': "[INFO] Ответ AI оборван. 'Хвост' обрезан до последнего разделителя для чистого доперевода."})

        # --- ЗАЩИТА ОТ ВЫРОЖДЕННОГО ХВОСТА ---
        # Если модель зациклилась и выдала тысячи копий одного абзаца, такой хвост
        # нельзя класть в payload: промпт до-генерации вернёт его модели, та продолжит
        # ту же петлю, и все оставшиеся попытки сгорят на валидации гарантированно.
        sanitized_partial = sanitize_partial_translation(partial_text)
        if not sanitized_partial:
            self.worker._post_event('log_message', {
                'message': (
                    "[WARN] Частичный ответ вырожден (модель зациклилась на повторах) — "
                    "хвост отброшен, задача будет переведена заново."
                )
            })
            return (task_id, self._payload_without_partial(task_payload))

        if sanitized_partial != partial_text:
            removed = len(partial_text) - len(sanitized_partial)
            self.worker._post_event('log_message', {
                'message': (
                    f"[INFO] Из частичного ответа вырезан зациклившийся хвост ({removed} симв.) "
                    "перед до-генерацией."
                )
            })
        partial_text = sanitized_partial

        # --- МУТАЦИЯ PAYLOAD ---
        # Хвост живёт в конце payload: у главы это элемент 3, у чанка — 8.
        # Раньше главу ради до-генерации переодевали в 'epub_chunk' 0/1, и она
        # так и оставалась в списке как «ЧАНК 1/1» до ручной пересборки задач.
        # Теперь до-перевод главы умеет EpubSingleFileProcessor, и глава
        # остаётся главой.
        partial_index = self._PARTIAL_INDEX_BY_TASK_TYPE.get(task_payload[0])
        if partial_index is None:
            return task_info

        base_payload_list = list(task_payload)
        previous_partial = ""
        if len(base_payload_list) > partial_index:
            stored_partial = base_payload_list[partial_index]
            previous_partial = stored_partial if isinstance(stored_partial, str) else ""
        base_payload_list = base_payload_list[:partial_index]

        if previous_partial:
            # PartialGenerationError несёт ТОЛЬКО что сгенерированный кусок —
            # продолжение, а не весь перевод. Если положить его в payload вместо
            # накопленного, в задаче останется фрагмент из середины главы, и любая
            # следующая до-генерация даст «середина + хвост», который валидатор
            # сравнит с целой главой и завалит. Поэтому накапливаем, а не заменяем.
            merged_partial, overlap_len = merge_partial_with_overlap_guard(
                previous_partial, partial_text
            )
            if len(merged_partial) <= len(previous_partial):
                # Попытка ничего не добавила — накопление встало. Продолжать
                # с тем же хвостом бессмысленно, переводим заново.
                self.worker._post_event('log_message', {
                    'message': (
                        "[WARN] До-генерация не продвинулась ни на символ — "
                        "накопленный хвост сброшен, задача будет переведена заново."
                    )
                })
                return (task_id, self._payload_without_partial(task_payload))

            self.worker._post_event('log_message', {
                'message': (
                    f"[INFO] Частичный перевод накоплен: "
                    f"{len(previous_partial)} + {len(partial_text)} симв."
                    + (f" (перекрытие {overlap_len} срезано)" if overlap_len else "")
                )
            })
            partial_text = merged_partial

        new_payload = tuple((*base_payload_list, partial_text))
        return (task_id, new_payload)
        
    def _handle_chunk_split(self, task_info, task_history):
        """
        Логика разделения большой задачи на чанки при критической ошибке (например, Context Overflow).
        """
        try:
            task_payload = task_info[1]
            task_type = task_payload[0]
            min_forced_chunk_size = api_config.min_forced_chunk_size()

            if task_type == 'epub':
                _, epub_path, chapter_path, *_ = task_payload

                with open(epub_path, 'rb') as f:
                    with zipfile.ZipFile(f, "r") as zf:
                        split_source = zf.read(chapter_path).decode("utf-8", "ignore")
                split_source = normalize_epub_chapter_heading_to_h1(split_source)

                prefix, chunks, suffix = brute_force_split(split_source)
            elif task_type == 'epub_chunk':
                _, epub_path, chapter_path, chunk_content, _, total_chunks, prefix, suffix, *_ = task_payload

                if total_chunks != 1:
                    raise ValueError("Нельзя безопасно доразбить один чанк внутри уже многочанковой главы.")

                if len(chunk_content.strip()) < (min_forced_chunk_size * 2):
                    raise ValueError("Текущий чанк слишком мал для повторного разделения.")

                split_source = f"{prefix}{chunk_content}{suffix}" if prefix or suffix else chunk_content
                _, chunks, _ = brute_force_split(split_source)
            else:
                raise ValueError(f"Тип задачи '{task_type}' не поддерживает принудительный split.")
            
            new_tasks = []
            for i, chunk_content in enumerate(chunks):
                # Формируем payload для типа 'epub_chunk'
                task_data = ('epub_chunk', epub_path, chapter_path, chunk_content, i, len(chunks), prefix, suffix)
                new_tasks.append(task_data)
            
            if new_tasks:
                # Наследование истории ошибок для предотвращения бесконечных циклов в чанках
                smart_history_to_pass = None
                parent_errors = task_history.get('errors', {})
                if parent_errors:
                    most_common_error = Counter(parent_errors).most_common(1)[0][0]
                    smart_history_to_pass = {'errors': {most_common_error: 1}}
                
                self.worker.task_manager.add_priority_tasks(
                    new_tasks,
                    parent_history=smart_history_to_pass,
                    parent_task_id=task_info[0],
                )
                self.worker._post_event('tasks_added', {'count': len(new_tasks)})

            return (task_info, False, 'SPLIT_FOR_RETRY', f"Разделено на {len(chunks)} частей")

        except Exception as split_exc:
            self.worker._post_event('log_message', {'message': f"[ERROR] Не удалось разделить задачу: {split_exc}"})
            return (task_info, False, 'CHUNK_ERROR', f"Ошибка разделения: {split_exc}")
