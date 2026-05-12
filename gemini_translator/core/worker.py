# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# worker.py (v4.0 - Strategy Pattern Refactor)
# ---------------------------------------------------------------------------
# Этот файл реализует архитектуру с единым универсальным воркером,
# который использует паттерн "Стратегия" для работы с различными API.
# ---------------------------------------------------------------------------
    
import time
import traceback

import sys
import threading
import contextlib
import contextvars

import uuid
import asyncio

from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, CancelledError
import concurrent.futures

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import QObject
# --- Импорты из сторонних библиотек ---

# --- Импорты из нашего проекта ---
from gemini_translator.api import config as api_config

from gemini_translator.utils.text import validate_html_structure

from gemini_translator.core.task_manager import ChapterQueueManager # Наш новый класс

# Импорт ошибок
from gemini_translator.api.errors import (
    ErrorType, WorkerAction, SuccessSignal, ContentFilterError, 
    OperationCancelledError, LocationBlockedError, ModelNotFoundError, 
    RateLimitExceededError, TemporaryRateLimitError, NetworkError, OperationCancelledError,
    GracefulShutdownInterrupt, ValidationFailedError, PartialGenerationError
)
# Импорт ресурсов потока
from gemini_translator.api.base import get_worker_loop

# Импорт Фабрики
from gemini_translator.api.factory import get_api_handler_class

# ============================================================================
#  Вспомогательные классы (PromptBuilder и ResponseParser и ErrorAnalyzer и RPMLimiter)
# ============================================================================
from .worker_helpers.error_analyzer import ErrorAnalyzer
from .worker_helpers.rpm_limiter import RPMLimiter

from .worker_helpers.prompt_builder import PromptBuilder
from .worker_helpers.response_parser import ResponseParser
from .worker_helpers.emerger_tasks import EmergencyTask

from .worker_helpers.task_factory import get_task_processor_class


_DEBUG_OPERATION_CONTEXT = contextvars.ContextVar("worker_debug_operation_context", default={})
WORKER_IDLE_WAKE_TIMEOUT_SECONDS = 2.0

# ============================================================================
#  ЕДИНЫЙ УНИВЕРСАЛЬНЫЙ КЛАСС-ВОРКЕР
# ============================================================================

class UniversalWorker(QObject):
    
    def __init__(self, **kwargs):
        super().__init__()
        
        app = QtWidgets.QApplication.instance()
        if not hasattr(app, 'event_bus'): raise RuntimeError("EventBus не найден.")
        self.bus = app.event_bus
        self._uses_topic_subscription = False
        self._worker_loop = None
        self._wake_event = None
        self._event_topics = (
            'stop_session_requested',
            'graceful_shutdown_requested',
            'cancel_graceful_shutdown',
            'tasks_added',
        )
        self._connect_to_bus()
        self.session_id = None
        self.worker_id = None
        # Флаги состояния (их инициализируем один раз, но при реините сбрасываем)
        self.is_shutting_down = False 
        self.is_cancelled = False
        self.emerger = EmergencyTask(self)
        # --- ЗАПУСК ПОЛНОЙ ИНИЦИАЛИЗАЦИИ ---
        # Передаем kwargs, так как при первом запуске это и есть конфиг
        self._initialize_worker_state(kwargs)
    
    def _post_event(self, name: str, data: dict = None):
        """
        Просто и безопасно отправляет событие в шину из любого потока.
        Qt сам позаботится о межпоточной доставке.
        """
        event = {
            'event': name,
            'source': f'worker_{self.worker_id}',
            'worker_key': self.api_key,
            'session_id': self.session_id,
            'data': data or {}
        }
        # Просто вызываем emit. Qt сделает все остальное.
        if hasattr(self.bus, "emit_event"):
            self.bus.emit_event(event)
        else:
            self.bus.event_posted.emit(event)

    def _connect_to_bus(self):
        if hasattr(self.bus, "subscribe"):
            for topic in self._event_topics:
                self.bus.subscribe(topic, self.on_event)
            self._uses_topic_subscription = True
        else:
            self.bus.event_posted.connect(self.on_event)

    def _disconnect_from_bus(self):
        if not getattr(self, 'bus', None):
            return
        try:
            if getattr(self, '_uses_topic_subscription', False) and hasattr(self.bus, "unsubscribe"):
                for topic in getattr(self, '_event_topics', ()):
                    self.bus.unsubscribe(topic, self.on_event)
            elif hasattr(self.bus, "event_posted"):
                self.bus.event_posted.disconnect(self.on_event)
        except (TypeError, RuntimeError, ValueError):
            pass

    def notify(self):
        """Будит асинхронный цикл воркера без активного polling."""
        loop = getattr(self, '_worker_loop', None)
        wake_event = getattr(self, '_wake_event', None)
        if loop is None or wake_event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake_event.set)
        except RuntimeError:
            pass

    @contextlib.contextmanager
    def debug_operation_context(self, operation_context: dict | None = None):
        token = _DEBUG_OPERATION_CONTEXT.set(dict(operation_context or {}))
        try:
            yield
        finally:
            _DEBUG_OPERATION_CONTEXT.reset(token)

    def get_debug_operation_context(self) -> dict:
        context_payload = _DEBUG_OPERATION_CONTEXT.get()
        if isinstance(context_payload, dict):
            return dict(context_payload)
        return {}
    
    def __del__(self):
        try:
            state = object.__getattribute__(self, '__dict__')
        except Exception:
            state = {}
        worker_id = state.get('worker_id')
        worker_suffix = worker_id[-4:] if isinstance(worker_id, str) and len(worker_id) >= 4 else "????"
        print(f"[WORKER LIFECYCLE] Поток {threading.get_native_id()} ЗАВЕРШИЛ РАБОТУ для воркера …{worker_suffix}.")
    
    def _initialize_worker_state(self, params: dict):
        """
        Инициализация состояния воркера.
        Сначала загружает ВСЕ переданные параметры как атрибуты,
        затем настраивает сложные компоненты.
        """
        app = QtWidgets.QApplication.instance()

        # --- 1. Приоритетная обработка Менеджеров (с Fallback на App) ---
        # Мы делаем это отдельно, чтобы если менеджер не передан в params, 
        # мы могли взять его из глобального app.
        
        self.settings_manager = params.get('settings_manager')
        if self.settings_manager is None:
            if hasattr(app, 'settings_manager'): 
                self.settings_manager = app.get_settings_manager()

        self.context_manager = params.get('context_manager')
        if self.context_manager is None:
            if hasattr(app, 'context_manager'): 
                self.context_manager = app.context_manager

        # TaskManager и ProjectManager обычно обязательны в params, но берем безопасно
        self.task_manager = params.get('task_manager', getattr(self, 'task_manager', None))
        self.project_manager = params.get('project_manager', getattr(self, 'project_manager', None))

        # --- 2. МАССОВЫЙ ИМПОРТ ПАРАМЕТРОВ ---
        # Копируем абсолютно всё из params в self.
        # Это гарантирует, что model_config, temperature, top_p и любые будущие настройки
        # автоматически станут доступны как self.model_config и т.д.
        
        # Исключаем менеджеры, чтобы не перезатереть логику fallback выше (хотя это не критично)
        exclude_keys = {'settings_manager', 'context_manager', 'task_manager', 'project_manager'}
        
        for key, value in params.items():
            if key not in exclude_keys:
                setattr(self, key, value)

        # --- 3. Нормализация специфических полей ---
        # (Приводим имена к внутреннему стандарту класса, если нужно)

        # RPM: приходит как 'rpm_limit', класс иногда хочет 'rpm_value'
        if hasattr(self, 'rpm_limit'):
            self.rpm_value = self.rpm_limit
        elif not hasattr(self, 'rpm_value'):
            self.rpm_value = 60

        # Brigade Size: приходит как 'max_concurrent_requests'
        if hasattr(self, 'max_concurrent_requests') and self.max_concurrent_requests and self.max_concurrent_requests > 0:
            self.brigade_size = self.max_concurrent_requests
        else:
            self.brigade_size = sys.maxsize

        # Error handlers and key accounting rely on a normalized model_id.
        if not getattr(self, 'model_id', None):
            model_config = getattr(self, 'model_config', None) or {}
            if isinstance(model_config, dict):
                self.model_id = model_config.get('id')

        # --- 4. Создание и настройка Компонентов ---
        
        # A. Limiter
        self.rpm_limiter = RPMLimiter(rpm_limit=self.rpm_value)

        # B. API Handler
        # Provider config берем из глобального конфига по имени
        self.provider_config = api_config.api_providers()[self.api_provider_name]
        handler_name = self.provider_config["handler_class"]
        try:
            handler_class = get_api_handler_class(handler_name)
        except ValueError as e:
            raise ValueError(f"Не удалось загрузить стратегию API: {e}")
        
        self.api_handler_instance = handler_class(self)
        

        # C. PromptBuilder
        self.prompt_builder = PromptBuilder(
            getattr(self, 'custom_prompt', ''), 
            self.context_manager, 
            getattr(self, 'use_system_instruction', False),
            sequential_mode=getattr(self, 'sequential_translation', False),
            project_manager=self.project_manager,
            provider_file_suffix=self.provider_config.get("file_suffix"),
            sequential_chapter_order=getattr(self, 'sequential_chapter_order', []),
            sequential_chain_starts=getattr(self, 'sequential_chain_starts', []),
            sequential_reference_char_limit=getattr(self, 'sequential_reference_char_limit', 60000),
        )

        # D. ResponseParser
        validator_function = validate_html_structure if not getattr(self, "force_accept", False) else None
        
        self.response_parser = ResponseParser(
            worker=self,
            log_callback=lambda msg: self._post_event('log_message', {'message': msg}),
            project_manager=self.project_manager,
            task_manager=self.task_manager,
            validator_func=validator_function,
            prompt_builder=self.prompt_builder
        )

        # E. ErrorAnalyzer
        self.error_analyzer = ErrorAnalyzer(self)
        
        # Сброс флагов завершения
        self.is_shutting_down = False
        self.is_cancelled = False
        
    @QtCore.pyqtSlot(dict)
    def on_event(self, event: dict):
        """
        Слушает шину. Обернуто в защиту от race-condition при уничтожении воркера.
        """
        try:
            # --- БЛОК 1: ПОЛУЧЕНИЕ И ВАЛИДАЦИЯ СОСТОЯНИЯ ВОРКЕРА ---
            
            # Пытаемся скопировать критичные данные в локальные переменные.
            # Если self уже разрушается и атрибутов нет -> вылетит в except AttributeError
            current_session_id = self.session_id 
            current_worker_id = self.worker_id 

            # ВАЖНО: Если атрибут есть, но он None или "", воркер - зомби.
            # Мы не должны обрабатывать никакие события, мы должны умереть.
            if not current_session_id:
                self.cancel()
                return 

            # --- БЛОК 2: ФИЛЬТРАЦИЯ СОБЫТИЯ ---
            
            incoming_session_id = event.get('session_id')

            # Если у события есть ID сессии, и он НЕ совпадает с нашим -> это не нам
            if incoming_session_id and incoming_session_id != current_session_id:
                return 
            
            # --- БЛОК 3: ОБРАБОТКА ЛОГИКИ ---
            
            event_name = event.get('event')

            # 1. Глобальная остановка
            if event_name == 'stop_session_requested':
                if not self.is_cancelled and not self.is_shutting_down:
                    self._post_event('log_message', {'message': f"[INFO] …{current_worker_id[-4:]} получил глобальную команду на остановку сессии."})
                self.cancel()
                return

            # 2. Реакция на персональную команду увольнения
            if event_name == 'graceful_shutdown_requested':
                target_worker_id = event.get('data', {}).get('target_worker_id')
                if target_worker_id == current_worker_id:
                    self._post_event('log_message', {'message': f"[INFO] …{current_worker_id[-4:]} получил приказ на грациозное завершение."})
                    self.initiate_graceful_shutdown()
                    self.notify()
            
            # 3. Реакция на персональную команду "оживления"
            if event_name == 'cancel_graceful_shutdown':
                target_worker_id = event.get('data', {}).get('target_worker_id')
                
                if target_worker_id == current_worker_id and self.is_shutting_down:
                    new_params = event.get('data', {}).get('new_params')
                    
                    if new_params:
                        self._post_event('log_message', {'message': f"♻️ Воркер …{current_worker_id[-4:]} начинает полную реинициализацию..."})
                        try:
                            self._initialize_worker_state(new_params)
                            self._post_event('log_message', {'message': f"✅ Воркер …{current_worker_id[-4:]} полностью переродился."})
                        except Exception as e:
                            self._post_event('log_message', {'message': f"❌ Ошибка реинициализации: {e}. Принудительная остановка."})
                            self.cancel()
                    else:
                        self.is_shutting_down = False
                        self.is_cancelled = False
                        self._post_event('log_message', {'message': f"✅ Воркер …{current_worker_id[-4:]} 'оживлен'."})
                    self.notify()

            if event_name == 'tasks_added':
                self.notify()

        except AttributeError:
            # СЮДА мы попадем, если у self отвалился session_id, worker_id или что-то еще
            # Это значит, поток в процессе уничтожения. Просто добиваем его.
            try:
                self.cancel() 
            except:
                pass # Если даже cancel не работает, значит всё уже умерло
            return

        except Exception as e:
            # Все остальные ошибки (логические, ValueError и т.д.) пробрасываем или логируем
            # raise e  # Раскомментировать, если нужно видеть реальные баги в консоли
            print(f"CRITICAL ERROR in worker on_event: {e}") # Лучше залогировать
            self.cancel()

    def _split_batch_after_content_filter(self, task_info, error_type):
        if error_type != ErrorType.CONTENT_FILTER:
            return False
        if not task_info or len(task_info) < 2:
            return False

        payload = task_info[1]
        if not payload or payload[0] != 'epub_batch':
            return False

        split_method = getattr(self.task_manager, 'split_in_progress_batch_into_chapters', None)
        if not split_method:
            return False

        if split_method(task_info, worker_id=self.worker_id, priority=1):
            self._post_event('log_message', {
                'message': (
                    "[BATCH FILTER] Content filter hit a batch. "
                    "The batch was split into individual chapters and returned to the queue."
                )
            })
            return True

        return False
            
    def run(self):
        """
        Основной метод воркера. Управляет event loop'ом, проактивно создает
        сессию и запускает основной цикл обработки задач.
        """
        import logging
        logging.getLogger('aiohttp').setLevel(logging.DEBUG)
        logging.getLogger('aiohttp_socks').setLevel(logging.DEBUG)
        
        loop = None
        try:
            # 1. Создаем и "захватываем" event loop для этого потока
            loop = get_worker_loop()
            self._worker_loop = loop
            self._wake_event = asyncio.Event()
            print(f"[WORKER LIFECYCLE] Поток {threading.get_native_id()} НАЧАЛ РАБОТУ для воркера …{self.worker_id[-4:]}…")
            
            # 2. Синхронная настройка
            client = self.client_map[self.api_key]
            # Передаем словарь настроек в хендлер
            if not self.api_handler_instance.setup_client(
                client_override=client, 
                proxy_settings=self.proxy_settings
            ):
                raise RuntimeError("Настройка API хендлера провалилась.")

            # 4. Прогрев (если нужен)
            if self.provider_config.get('needs_warmup', False) and getattr(self, 'use_warmup', False):
                self._post_event('log_message', {'message': f"⏳ [WARMUP] Запуск ритуала-приветствия для ключа …{self.api_key[-4:]}…"})
                warmup_success = loop.run_until_complete(self._perform_warmup())
                if not warmup_success: return
            
            # 5. Запуск основного цикла обработки задач
            loop.run_until_complete(self._async_processing_loop())
    
        except Exception as e:
            # Ловим стандартные ошибки
            print(e)
            error_msg = f"[FATAL WORKER ERROR] …{self.worker_id[-4:]}: {type(e).__name__}: {e}"
            self._post_event('log_message', {'message': error_msg})
        
        except BaseException as e:
            # ЛОВИМ ВСЁ ОСТАЛЬНОЕ (CancelledError, SystemExit и т.д.)
            # Это тот самый блок, который поймает критический сбой asyncio/системы
            error_msg = f"[CRITICAL SYSTEM STOP] …{self.worker_id[-4:]}: {type(e).__name__}: {e}"
            self._post_event('log_message', {'message': error_msg})
            
        finally:
            # 6. ГАРАНТИРОВАННАЯ ОЧИСТКА РЕСУРСОВ
            if loop and not loop.is_closed():
                # Закрытием сессии управляет обработчик
                loop.run_until_complete(self.api_handler_instance._close_thread_session_internal())
                loop.close()
            self._worker_loop = None
            self._wake_event = None
            
            self.cancel()

            print(f"[WORKER LIFECYCLE] Поток {threading.get_native_id()} ЗАВЕРШИЛ РАБОТУ для воркера …{self.worker_id[-4:]}.")


    def initiate_graceful_shutdown(self):
        """
        Слот, который переводит воркер в режим "мягкого увольнения".
        Он перестанет брать новые задачи.
        """
        if not self.is_shutting_down:
            self._post_event('log_message', {'message': f"[WARN] Воркер …{self.worker_id[-4:]} (ключ: {self.api_key[-4:]}) переведен в режим завершения. Доделывает взятые задачи…"})
        self.is_shutting_down = True
    
    
    async def _async_processing_loop(self):
        """
        Асинхронный "мозг" воркера. Управляет параллельными задачами,
        не блокируя добавление новых задач.
        """
        concurrency_limit = self.brigade_size if self.brigade_size != sys.maxsize else 1000
        active_tasks = set()


        while not self.is_cancelled:
            # --- ШАГ 1: "Сбор урожая" - убираем завершенные задачи ---
            
            done_tasks = {t for t in active_tasks if t.done()}
            for task in done_tasks:
                try:
                    # Вызов .result() либо вернет результат, либо возбудит исключение задачи.
                    # Это "потребляет" исключение, предотвращая предупреждение asyncio.
                    task.result()
                except GracefulShutdownInterrupt:
                    # Это штатный сигнал для остановки.
                    pass
                except (CancelledError, SuccessSignal, asyncio.CancelledError):
                    # FIX: Добавлено asyncio.CancelledError, так как CancelledError из concurrent.futures 
                    # не ловит отмену нативных асинхронных задач Python 3.8+
                    pass
                except Exception as exc:
                    # А вот это - настоящая, непредвиденная ошибка, о которой нужно сообщить.
                    self._post_event('log_message', {'message': f"[ERROR] Неперехваченное исключение в задаче: {exc}"})
            active_tasks.difference_update(done_tasks)
            if not self.check_session():
                break
            # --- ШАГ 2: "Посадка" - добавляем новые задачи, пока есть место и работа ---
            while len(active_tasks) < concurrency_limit:
                if self.is_shutting_down or not self.task_manager.has_pending_tasks():
                    break # Выходим, если увольняемся или нет работы
                if not self.check_session():
                    break
                if not self.rpm_limiter.can_proceed():
                    break # Выходим, если уперлись в RPM

                task_info = self.task_manager.get_next_task(self.worker_id)
                if task_info:
                    task = asyncio.create_task(self._process_single_task_with_retries(task_info))
                    active_tasks.add(task)
                    self._post_event('log_message', {'message': f"Ключ …{self.api_key[-4:]} взял задачу."})
                else:
                    # Задачи в очереди кончились
                    break

            # --- ШАГ 3: Условие выхода из основного цикла ---
            if self.is_shutting_down and not active_tasks:
                break
            if not active_tasks and not self.task_manager.has_pending_tasks():
                break

            # --- ШАГ 4: Засыпаем до реального события, а не крутим polling. ---
            await self._wait_for_next_cycle(active_tasks)
        
        if not self.check_session():
            # Обнаружена смена сессии. Этот воркер — "зомби".
            self._post_event('log_message', {'message': "[FINISHING] Обнаружена смена режима. Воркер самоуничтожается."})
            self.cancel() # Устанавливаем флаг, чтобы другие части кода знали о завершении
        
        # --- ШАГ 5 (опционально): Дожидаемся оставшихся задач при graceful shutdown ---
        if active_tasks:
            # Используем asyncio.gather для сбора всех результатов и исключений.
            results = await asyncio.gather(*active_tasks, return_exceptions=True)
            for result in results:
                # FIX: Проверяем и на asyncio.CancelledError
                if isinstance(result, Exception) and not isinstance(result, (GracefulShutdownInterrupt, CancelledError, SuccessSignal, asyncio.CancelledError)):
                    self._post_event('log_message', {'message': f"[ERROR] Ошибка в фоновой задаче при завершении: {result}"})

    async def _wait_for_next_cycle(self, active_tasks, timeout=WORKER_IDLE_WAKE_TIMEOUT_SECONDS):
        wake_event = getattr(self, '_wake_event', None)
        waitables = set(active_tasks)
        wake_task = None

        if wake_event is not None:
            wake_task = asyncio.create_task(wake_event.wait())
            waitables.add(wake_task)

        if not waitables:
            await asyncio.sleep(timeout)
            return

        done, pending = await asyncio.wait(
            waitables,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if wake_task is not None:
            if wake_task in pending:
                wake_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wake_task
            elif wake_task in done:
                wake_event.clear()


    def check_session(self):
        if not self.session_id:
            self.cancel()
            return False
        try:
            if self.bus and hasattr(self.bus, '_data_store'):
                if 'current_active_session' in self.bus._data_store.keys():
                    if not self.bus.get_data('current_active_session') == self.session_id:
                        self.cancel()
                        return False
                else:
                    self.cancel()
                    return False
        except Exception as e:
            print(e)
            self.cancel()
            return False
        
        try:
            if not self.api_key_manager.check_key(self.api_key):
                self.initiate_graceful_shutdown()
                return True
                
            keys_map = self.api_key_manager.get_map()
            if not keys_map.get(self.api_key) == self.worker_id:
                self.initiate_graceful_shutdown()
                return True
            if not keys_map.get(self.worker_id) == self.api_key:
                self.initiate_graceful_shutdown()
                return True
        except:
            pass
        
        return True

    async def _process_single_task_with_retries(self, task_info):
        """
        АСИНХРОННАЯ версия. Обрабатывает одну задачу, включая логику повторов и анализа ошибок.
        По завершении (успех/провал) вызывает _handle_task_result.
        """
        if not task_info:
            self._handle_task_result((None, True, 'IGNORED_NONE', 'Получена пустая задача.'))
            return
    
        task_history = self.task_manager.get_failure_history(task_info)
    
        try:
            # _execute_task теперь тоже async, так как вызывает async-хендлеры
            result = await self._execute_task(task_info)
            self._handle_task_result(result)
    
        except SuccessSignal as signal:
            self._handle_task_result((task_info, True, signal.status_code, signal.message))
        
        except GracefulShutdownInterrupt:
            # Это исключение используется для прерывания работы воркера
            self.is_shutting_down = True
            self.task_manager.task_requeued(self.worker_id, task_info)
            raise # Пробрасываем выше, чтобы цикл обработки завершился
    
        except Exception as exc:
            action, error_type, original_exc = self.error_analyzer.analyze_and_act(exc, task_info, task_history)

            if self._split_batch_after_content_filter(task_info, error_type):
                return
    
            if action == WorkerAction.ABORT_WORKER:
                self.is_shutting_down = True
                # Не переставляем задачу, она останется в in_progress и будет возвращена при перезапуске
                raise GracefulShutdownInterrupt()
    
            elif action in (WorkerAction.RETRY_COUNTABLE, WorkerAction.RETRY_NON_COUNTABLE):
                task_to_requeue = self.emerger._mutate_task_for_completion(task_info, original_exc)
                status = 'REQUEUED_COUNTABLE' if action == WorkerAction.RETRY_COUNTABLE else 'REQUEUED_NON_COUNTABLE'
                self._handle_task_result((task_to_requeue, False, status, f'Возврат в очередь ({error_type.name})'))
    
            elif action == WorkerAction.FAIL_AND_ATTEMPT_CHUNK:
                split_result = self.emerger._handle_chunk_split(task_info, task_history)
                self._handle_task_result(split_result)

            elif action == WorkerAction.FAIL_PERMANENTLY:
                final_status_type = 'filtered' if error_type == ErrorType.CONTENT_FILTER else 'PERMANENT_FAILURE'
                error_message = f"Окончательный провал ({error_type.name})"
                self._handle_task_result((task_info, False, final_status_type, error_message))
    
    def _handle_task_result(self, result_tuple):
        """
        Централизованно обрабатывает результат выполнения задачи (успех/провал/перепостановка),
        обновляет TaskManager и отправляет событие в шину.
        """
        try:
            returned_task_info, success, status_type, message = result_tuple
            if returned_task_info is None: return

            is_requeued = 'REQUEUED' in status_type

            if is_requeued:
                if 'NON_COUNTABLE' in status_type:
                    self.task_manager.task_requeued(self.worker_id, returned_task_info) # Обычный возврат в общую очередь
                else:
                    self.task_manager.task_requeued_for_retry(self.worker_id, returned_task_info) # Возврат в приоритетную очередь
            else:
                task_info_for_event, result_data_for_event = None, None
                if success and isinstance(returned_task_info, tuple) and len(returned_task_info) == 2 and isinstance(returned_task_info[0], tuple):
                    task_info_for_event, result_data_for_event = returned_task_info
                else:
                    task_info_for_event = returned_task_info
                
                task_id, task_payload = task_info_for_event
                task_type = task_payload[0]

                if success:
                    self.error_analyzer.network_warnings = 0
                    self._post_event('api_connection_healthy')
                    
                    # Для 'hello_task' не делаем ничего, просто считаем успехом
                    if task_type == 'hello_task':
                        return 
                    
                    if task_type in ('epub_chunk', 'raw_text_translation'):
                        self.task_manager.task_done_with_content(
                            self.worker_id, task_info_for_event, result_data_for_event, self.api_provider_name
                        )
                    
                    else:
                        self.task_manager.task_done(self.worker_id, task_info_for_event, result_data_for_event)
                else:
                    self.task_manager.task_failed_permanently(self.worker_id, task_info_for_event)

                event_data = {
                    'task_info': task_info_for_event,
                    'result_data': result_data_for_event,
                    'success': success,
                    'error_type': status_type,
                    'message': message
                }
                self._post_event('task_finished', event_data)

        except Exception as e:
            tb = traceback.format_exc()
            self._post_event('log_message', {'message': f"[CRITICAL] Ошибка в _handle_task_result: {e}\n{tb}"})

    async def _execute_task(self, task_info):
        """
        Умный маршрутизатор.
        Версия 3.0: Учитывает PartialGenerationError как счетную ошибку,
        но сохраняет "второй шанс" для больших задач.
        """
        task_id, task_payload = task_info
        task_type = task_payload[0]
        task_history = self.task_manager.get_failure_history(task_info)
        
        # --- ИЗМЕНЕНИЕ 1: Теперь failure_count - это просто общее число ошибок ---
        failure_count = task_history.get('total_count', 0)
        
        task_name = self.task_manager._get_task_display_name(task_payload)

        # --- ИЗМЕНЕНИЕ 2: Упрощенная логика смены стратегии ---
        default_stream_mode = {
            'epub_batch': False, 'epub': True, 'epub_chunk': False,
            'glossary_batch_task': True, 'raw_text_translation': True
        }.get(task_type, True)

        use_stream = default_stream_mode
        should_log_strategy = False

        # Для "больших" задач (пакеты и целые главы)
        if task_type in ('epub', 'glossary_batch_task'):
            # Меняем стратегию только после ВТОРОЙ ошибки (failure_count >= 2).
            # То есть на 3-й и 4-й попытках пробуем альтернативный метод.
            if failure_count >= 2:
                # Если кол-во ошибок 2 или 3 (3-я и 4-я попытки) - инвертируем.
                # Если 4 или 5 (5-я и 6-я, если лимит позволит) - возвращаем дефолт.
                # Логика: (2//2)%2=1 (меняем), (3//2)%2=1 (меняем), (4//2)%2=0 (не меняем)
                if (failure_count // 2) % 2 != 0:
                    use_stream = not default_stream_mode
                
                should_log_strategy = True
            
            # Если failure_count == 1 (вторая попытка), мы сюда не заходим,
            # use_stream остается default (STREAM), и лог стратегии не пишется (как и должно быть).

        # Для "маленьких" задач (чанки и др.)
        else:
            # Меняем стратегию с ПЕРВОЙ же ошибки (т.е. когда failure_count >= 1)
            if failure_count > 0:
                # 1 ошибка (2 попытка) -> меняем
                # 2 ошибки (3 попытка) -> возвращаем дефолт
                if failure_count % 2 != 0:
                    use_stream = not default_stream_mode
                should_log_strategy = True
        
        if should_log_strategy:
            mode_text = "STREAM" if use_stream else "SINGLE"
            # failure_count - это количество уже произошедших ошибок.
            # Значит, текущая попытка - это failure_count + 1.
            self._post_event('log_message', {
                'message': f"⚙️ [STRATEGY] Задача '{task_name}': Попытка #{failure_count + 1}. Выбран режим API: {mode_text}"
            })
        
        # --- Делегирование выполнения стратегии ---
        processor_class = get_task_processor_class(task_type)
        if not processor_class:
            raise TypeError(f"Неизвестный тип задачи: {task_type}")
            
        # Создаем экземпляр стратегии, передавая ему себя (воркера) как контекст
        processor_instance = processor_class(self)
        
        # Вызываем метод execute у конкретной стратегии
        return await processor_instance.execute(task_info, use_stream=use_stream)

    async def _perform_warmup(self) -> bool:
        """
        Выполняет асинхронный "прогрев" API.
        Возвращает True в случае успеха, False в случае провала.
        """
        hello_task_info = (uuid.uuid4(), ('hello_task',))
        self.rpm_limiter.can_proceed()  # Сбрасываем таймер RPM перед первым запросом
    
        try:
            # _process_single_task_with_retries теперь async и сам управляет своей логикой
            await self._process_single_task_with_retries(hello_task_info)
            # Если метод завершился без исключений SuccessSignal или других,
            # это значит, что он отправил результат в _handle_task_result.
            # Для hello_task успешное завершение без исключений - это успех.
            self._post_event('log_message', {'message': f"🔥 [WARMUP] Ритуал-приветствие для ключа …{self.api_key[-4:]} завершен."})
            return True
        except SuccessSignal:
            # Исключение SuccessSignal от hello_task также является успехом
            self._post_event('log_message', {'message': f"🔥 [WARMUP] Ритуал-приветствие для ключа …{self.api_key[-4:]} завершен."})
            return True
        except Exception as e:
            # Любое другое исключение - это провал.
            # analyze_and_act уже залогировал причину, нам не нужно дублировать.
            self._post_event('log_message', {'message': f"🔻 [WARMUP] Ритуал-приветствие для ключа …{self.api_key[-4:]} провален. Воркер будет остановлен."})
            return False


    def cancel(self):
        """Публичный метод для внешней отмены операций воркера с полной очисткой."""
        # Устанавливаем флаг, который проверяется в основном асинхронном цикле
        self.is_cancelled = True
        self.notify()
        self._disconnect_from_bus()
        
        try:
            self.task_manager.rescue_task_by_worker_id(self.worker_id)
        except:
            pass
