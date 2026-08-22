# Импорт перечислений для классификации ошибок и команд
from gemini_translator.api.errors import ErrorType, WorkerAction

# Импорт конкретных классов исключений для их анализа
from gemini_translator.api.errors import (
    ContentFilterError,
    LocationBlockedError,
    ModelNotFoundError,
    RateLimitExceededError,
    TemporaryRateLimitError,
    NetworkError,
    OperationCancelledError,
    ValidationFailedError,
    PartialGenerationError
)


class ErrorAnalyzer:
    INFINITE_RETRY_PACKAGE_TYPES = {'epub_batch', 'glossary_batch_task'}
    TERMINAL_PACKAGE_ERRORS = {ErrorType.CONTENT_FILTER, ErrorType.API_ERROR, ErrorType.VALIDATION}
    
    # --- Конфигурация правил отказов ---
    FAILURE_RULES = {
        ErrorType.PARTIAL_GENERATION:   {'max_attempts': 3, 'allows_chunking': True},
        ErrorType.VALIDATION:           {'max_attempts': 6, 'max_total_attempts': 6, 'allows_chunking': True},
        ErrorType.NETWORK:              {'max_attempts': 2, 'allows_chunking': False},
        ErrorType.CONTENT_FILTER:       {'max_attempts': 2, 'allows_chunking': False},
        ErrorType.API_ERROR:            {'max_attempts': 2, 'allows_chunking': True},
        ErrorType.CANCEL:               {'max_attempts': 0, 'allows_chunking': False},
    }
    # Общий лимит РАЗНЫХ типов ошибок
    TOTAL_ATTEMPTS_LIMIT = 4

    
    def __init__(self, worker_instance):
        self.worker = worker_instance
        self.task_manager = self.worker.task_manager
        self.network_warnings = 0

    def _build_error_details(self, task_name: str, error_type: ErrorType, exc=None):
        if exc is None:
            return None

        sections = [
            f"Задача: {task_name}",
            f"Тип ошибки: {error_type.name}",
            f"Класс исключения: {type(exc).__name__}",
            f"Сообщение: {str(exc)}",
        ]

        reason = getattr(exc, 'reason', None)
        if reason:
            sections.append(f"Причина: {reason}")

        delay_seconds = getattr(exc, 'delay_seconds', None)
        if delay_seconds is not None:
            sections.append(f"Пауза/задержка: {delay_seconds} сек.")

        cause = getattr(exc, '__cause__', None) or getattr(exc, '__context__', None)
        if cause:
            sections.append(f"Связанная ошибка: {type(cause).__name__}: {cause}")

        raw_package_text = getattr(exc, 'raw_package_text', '') or ''
        if raw_package_text.strip():
            sections.append("Полученный пакет/сырой ответ:")
            sections.append(raw_package_text)

        partial_text = getattr(exc, 'partial_text', '') or ''
        if partial_text.strip():
            sections.append("Частичный ответ:")
            sections.append(partial_text)

        details_text = "\n\n".join(section for section in sections if section)
        if not details_text.strip():
            return None

        return {
            'details_title': f"Детали ошибки для '{task_name}'",
            'details_text': details_text,
        }
        
    def analyze_and_act(self, exc, task_info: tuple, task_history: dict):
        task_id, task_payload = task_info
        task_name = self.task_manager._get_task_display_name(task_payload)
        is_package_task = bool(task_payload) and task_payload[0] in self.INFINITE_RETRY_PACKAGE_TYPES
        worker_model_id = getattr(self.worker, 'model_id', None)

        # --- Шаг 1: Определяем исходный тип ошибки и тип для записи в историю ---
        error_for_rules = self._classify_exception(exc)
        error_for_history = error_for_rules
        # --- Шаг 2: "Умная" переклассификация и эскалация ---
        if error_for_rules == ErrorType.PARTIAL_GENERATION:
            partial_text = getattr(exc, 'partial_text', '')
            reason = getattr(exc, 'reason', 'OTHER').upper()
            is_first_attempt = (
                (task_payload[0] == 'epub' and len(task_payload) <= 3)
                or (task_payload[0] == 'epub_chunk' and len(task_payload) <= 8)
            )

            if not partial_text.strip() and is_first_attempt:
                # ЭСКАЛАЦИЯ: Пустой хвост на первой попытке. Перезаписываем ошибку для правил.
                new_error = ErrorType.CONTENT_FILTER if reason in ["SAFETY", "PROHIBITED_CONTENT"] else ErrorType.API_ERROR
                error_for_rules = new_error
                error_for_history = new_error
            elif reason in ["SAFETY", "PROHIBITED_CONTENT"]:
                # ПЕРЕКЛАССИФИКАЦИЯ ДЛЯ ИСТОРИИ: Хвост есть, но причина - фильтр.
                error_for_history = ErrorType.CONTENT_FILTER
        
        # --- Шаг 3: Получаем правила для error_for_rules ---
        rule = self.FAILURE_RULES.get(error_for_rules, {'max_attempts': 1})
        max_attempts = rule['max_attempts']
        max_total_attempts = rule.get('max_total_attempts', self.TOTAL_ATTEMPTS_LIMIT)

        # --- Шаг 4: Обработка не-счетных и фатальных ошибок (используем error_for_rules) ---
        if error_for_rules in [ErrorType.GEOBLOCK, ErrorType.QUOTA_EXCEEDED, ErrorType.MODEL_NOT_FOUND]:
            payload = {"type": error_for_rules.name.lower(), "model_id": worker_model_id, "exception": exc}
            self.worker._post_event('fatal_error', {'payload': payload})
            return WorkerAction.ABORT_WORKER, error_for_rules, exc

        if error_for_rules == ErrorType.TEMPORARY_LIMIT:
            delay = getattr(exc, 'delay_seconds', 61)
            current_rpm = self.worker.rpm_limiter.get_rpm()
            self.worker.rpm_limiter.decrease_rpm(percentage=25)
            new_rpm = self.worker.rpm_limiter.get_rpm()
            self.worker._post_event('temporary_limit_warning_received', {'delay_seconds': delay, 'original_exception': exc, "model_id": worker_model_id})
            self.worker.rpm_limiter.update_last_request_time(delay)
            log_message = (f"🟡 API запросил паузу для ключа …{self.worker.worker_id[-4:]} на {delay} секунд.")
            if current_rpm > new_rpm:
                log_message += (f"\n    ➡️ Действие: RPM автоматически снижен с {current_rpm} до {new_rpm}.")
            self.worker._post_event('log_message', {'message': log_message})
            return WorkerAction.RETRY_NON_COUNTABLE, error_for_rules, exc
        
        if error_for_rules == ErrorType.NETWORK:
            self.network_warnings = self.network_warnings + 1
            delay = getattr(exc, 'delay_seconds', 30) * (self.network_warnings)
            # Delay the next request attempt without freezing the task.
            # Do not lower RPM because of a network glitch.
            self.worker.rpm_limiter.update_last_request_time(delay)
            self.worker._post_event('temporary_limit_warning_received', {'delay_seconds': delay, 'original_exception': exc, "model_id": worker_model_id})
            self._record_and_log_failure(task_info, error_for_history, exc)
            return WorkerAction.RETRY_COUNTABLE, error_for_history, exc

        if error_for_rules in [ErrorType.VALIDATION, ErrorType.CONTENT_FILTER, ErrorType.PARTIAL_GENERATION]:
            self.network_warnings = 0
            self.worker._post_event('api_connection_healthy')

        # Опция «Не повторять блокировки»: глава/чанк, заблокированные
        # контент-фильтром (включая partial с reason SAFETY/PROHIBITED_CONTENT),
        # проваливаются сразу — без повторных отправок, которые приводят к
        # усечению текста при до-генерации. Для epub_batch воркер сам разбивает
        # пакет (_split_batch_after_content_filter имеет приоритет), поэтому
        # для пакетов это решение игнорируется и безопасно.
        if getattr(self.worker, 'skip_content_filter_retry', False) and (
            error_for_rules == ErrorType.CONTENT_FILTER
            or error_for_history == ErrorType.CONTENT_FILTER
        ):
            self._record_and_log_failure(task_info, ErrorType.CONTENT_FILTER, exc)
            return self._log_and_fail_permanently(task_name, ErrorType.CONTENT_FILTER, exc)

        if error_for_rules == ErrorType.CANCEL:
            self._record_and_log_failure(task_info, error_for_history, exc)
            return WorkerAction.RETRY_NON_COUNTABLE, error_for_rules, exc

        if (
            is_package_task
            and error_for_rules not in self.TERMINAL_PACKAGE_ERRORS
            and error_for_history not in self.TERMINAL_PACKAGE_ERRORS
        ):
            self._record_and_log_failure(task_info, error_for_history, exc)
            self.worker._post_event('log_message', {
                'message': f"[PACKAGE] Бесконечный ретрай для '{task_name}' (тип ошибки: {error_for_history.name})."
            })
            return WorkerAction.RETRY_NON_COUNTABLE, error_for_history, exc

        # --- Шаг 5: Принятие решения на основе СЧЕТНЫХ ошибок ---
        # Считаем попытки по error_for_rules.
        # Если error_for_history отличается, СУММИРУЕМ их счетчики,
        # чтобы учесть оба случая в общем лимите для текущего типа ошибки.
        current_type_count = task_history.get('errors', {}).get(error_for_rules.name, 0)
        if error_for_rules.name != error_for_history.name:
            current_type_count += task_history.get('errors', {}).get(error_for_history.name, 0)
            
        total_attempts_count = task_history.get('total_count', 0)

        if (current_type_count + 1 >= max_attempts) or \
           (total_attempts_count + 1 >= max_total_attempts):
            
            if total_attempts_count + 1 >= max_total_attempts:
                self.worker._post_event('log_message', {'message': f"[ANALYZER] Превышен общий лимит ({max_total_attempts}) попыток для задачи '{task_name}'."})
            
            # Записываем в историю финальный, самый точный тип ошибки
            self._record_and_log_failure(task_info, error_for_history, exc)
            return self._decide_final_action(task_name, task_payload, task_history, exc, error_for_history)
        
        # --- Шаг 6: Если все лимиты в норме, даем команду на повтор ---
        self._record_and_log_failure(task_info, error_for_history, exc)
        return WorkerAction.RETRY_COUNTABLE, error_for_history, exc

    def _record_and_log_failure(self, task_info: tuple, error_type: ErrorType, exc=None):
        """Атомарно записывает ошибку в БД и выводит в лог красивое сообщение."""
        if error_type == ErrorType.CANCEL or error_type == ErrorType.GEOBLOCK or error_type == ErrorType.MODEL_NOT_FOUND:
            return
        # 1. Записываем в БД
        self.task_manager.record_failure(task_info, error_type.name)
        
        # 2. Готовим красивый лог
        task_name = self.task_manager._get_task_display_name(task_info[1])
        history = self.task_manager.get_failure_history(task_info)
        total_count = history.get('total_count', 0)

        log_message = ""
        if error_type == ErrorType.CONTENT_FILTER:
            log_message = f"🛡️ ФИЛЬТР: Зарегистрирована блокировка контента для '{task_name}' (всего: {total_count})."
        elif error_type == ErrorType.VALIDATION:
            log_message = f"📋 ВАЛИДАЦИЯ: Зарегистрирована ошибка структуры ответа для '{task_name}' (всего: {total_count})."
        else:
            # Стандартное сообщение для всех остальных ошибок
            log_message = f"[TASK] Зарегистрирована ошибка для '{task_name}' (тип: {error_type.name}, всего: {total_count})."
        
        log_payload = {'message': log_message}
        error_details = self._build_error_details(task_name, error_type, exc)
        if error_details:
            log_payload.update(error_details)

        self.worker._post_event('log_message', log_payload)

    def _decide_final_action(self, task_name, task_payload: tuple, task_history: dict, last_exc, final_error_type: ErrorType):
        """
        ФИНАЛЬНАЯ ВЕРСИЯ. Правильно определяет чанки и запрещает для них "План Б".
        Безопасно получает chunk_on_error.
        Версия 11.0: Расформировывает проваленные пакеты.
        """
        task_type = task_payload[0]
    
        chunk_on_error_enabled = getattr(self.worker, 'chunk_on_error', False)
        
        is_chunkable_task_type = (task_type == 'epub')
        if task_type == 'epub_chunk':
            total_chunks = task_payload[5] if len(task_payload) > 5 else None
            is_chunkable_task_type = (total_chunks == 1)
        was_force_chunked = task_history.get('force_chunked', False)
    
        if not (is_chunkable_task_type and chunk_on_error_enabled) or was_force_chunked:
            return self._log_and_fail_permanently(task_name, final_error_type, last_exc)
    
        error_names_in_history = task_history.get('errors', {}).keys()
        
        can_try_chunking = False
        if error_names_in_history:
            for error_name in error_names_in_history:
                try:
                    rule = self.FAILURE_RULES.get(ErrorType[error_name])
                    if rule and rule.get('allows_chunking', False):
                        can_try_chunking = True
                        break # Достаточно одного разрешения
                except (KeyError, AttributeError): pass
        
        if can_try_chunking:
            log_message = (
                f"❗️ПРОВАЛ ПОПЫТОК для '{task_name}'.\n"
                f"    Последняя ошибка: ({self._classify_exception(last_exc).name}): {str(last_exc)}\n"
                f"    ➡️ Действие: Запуск ПЛАНА Б (принудительное разделение на части)."
            )
            log_payload = {'message': log_message}
            error_details = self._build_error_details(task_name, final_error_type, last_exc)
            if error_details:
                log_payload.update(error_details)
            self.worker._post_event('log_message', log_payload)
            return WorkerAction.FAIL_AND_ATTEMPT_CHUNK, self._classify_exception(last_exc), last_exc
        else:
            return self._log_and_fail_permanently(task_name, final_error_type, last_exc)

    def _log_and_fail_permanently(self, task_name, error_type, exc):
        """Вспомогательная функция для логирования и возврата окончательного провала."""
        # --- Лаконичные сообщения для конкретных ошибок ---

        if error_type == ErrorType.CONTENT_FILTER:
            log_message = f"🛡️ ФИЛЬТР: Задача '{task_name}' окончательно заблокирована политикой безопасности."
        elif error_type == ErrorType.VALIDATION:
            log_message = f"📋 ВАЛИДАЦИЯ: Задача '{task_name}' провалена из-за структурных ошибок в ответе API: {str(exc)}"
        elif error_type == ErrorType.NETWORK:
            log_message = f"[NETWORK] Задача '{task_name}' временно заморожена до восстановления сети: {str(exc)}"
        else:
            # Стандартное подробное сообщение для всех остальных ошибок
            log_message = (
                f"❌ ОКОНЧАТЕЛЬНЫЙ ПРОВАЛ ЗАДАЧИ: '{task_name}'\n"
                f"    Последняя ошибка ({error_type.name}): {str(exc)}"
            )
        if error_type == ErrorType.CANCEL or error_type == ErrorType.GEOBLOCK or error_type == ErrorType.MODEL_NOT_FOUND:
            return WorkerAction.RETRY_NON_COUNTABLE, error_type, exc


        log_payload = {'message': log_message}
        error_details = self._build_error_details(task_name, error_type, exc)
        if error_details:
            log_payload.update(error_details)
        self.worker._post_event('log_message', log_payload)
        if error_type == ErrorType.NETWORK:
            return WorkerAction.RETRY_COUNTABLE, error_type, exc
        return WorkerAction.FAIL_PERMANENTLY, error_type, exc

    def _classify_exception(self, exc) -> ErrorType:
        if isinstance(exc, NetworkError):
            return ErrorType.NETWORK
        if isinstance(exc, ContentFilterError):
            return ErrorType.CONTENT_FILTER
        if isinstance(exc, LocationBlockedError):
            return ErrorType.GEOBLOCK
        if isinstance(exc, RateLimitExceededError):
            return ErrorType.QUOTA_EXCEEDED
        if isinstance(exc, PartialGenerationError):
            return ErrorType.PARTIAL_GENERATION
        if isinstance(exc, TemporaryRateLimitError):
            return ErrorType.TEMPORARY_LIMIT
        if isinstance(exc, ModelNotFoundError):
            return ErrorType.MODEL_NOT_FOUND
        if isinstance(exc, ValidationFailedError):
            return ErrorType.VALIDATION
        if isinstance(exc, OperationCancelledError):
            return ErrorType.CANCEL

        # ----------------------------------------------

        return ErrorType.API_ERROR
