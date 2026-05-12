# gemini_translator/core/translation_engine.py

import os
import re
import time
import traceback
import uuid
import zipfile
import threading

from PyQt6 import QtWidgets, QtCore 
from PyQt6.QtCore import QObject, pyqtSignal, QThread, pyqtSlot
from PyQt6.QtWidgets import QMessageBox
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, CancelledError

from ..api import config as api_config
from ..core.task_manager import ChapterQueueManager
from ..core.worker import UniversalWorker
from ..utils.project_manager import TranslationProjectManager
from ..utils.helpers import check_value 
from ..api.managers import ApiKeyManager
from ..core.chunk_assembler import ChunkAssembler

def normalize_sequential_parallel_settings(settings: dict, log_callback=None):
    if not settings.get('sequential_translation'):
        return

    try:
        requested_splits = int(settings.get('sequential_translation_splits', 1) or 1)
    except (TypeError, ValueError):
        requested_splits = 1
    requested_splits = max(1, requested_splits)
    try:
        requested_workers = int(settings.get('num_instances', 1) or 1)
    except (TypeError, ValueError):
        requested_workers = 1
    try:
        requested_concurrency = int(settings.get('max_concurrent_requests', 1) or 1)
    except (TypeError, ValueError):
        requested_concurrency = 1
    requested_concurrency = max(1, requested_concurrency)

    model_config = settings.get('model_config') if isinstance(settings.get('model_config'), dict) else {}
    provider_id = str(settings.get('provider') or model_config.get('provider') or "").strip()
    if provider_id == "workascii_chatgpt":
        desired_concurrency = max(requested_splits, requested_concurrency)
        if requested_workers != 1 or requested_concurrency != desired_concurrency:
            if log_callback:
                log_callback(
                    "[SEQUENTIAL] ChatGPT Web uses one browser worker with "
                    f"{desired_concurrency} parallel page(s), matching work_ascii."
                )
        settings['num_instances'] = 1
        settings['max_concurrent_requests'] = desired_concurrency
        return

    if (
        requested_workers != requested_splits
        or requested_concurrency != 1
    ):
        if log_callback:
            log_callback(
                "[SEQUENTIAL] Sequential chapter translation forces "
                f"ordered execution: {requested_splits} chain(s), "
                "1 in-worker request per worker."
            )
    settings['num_instances'] = requested_splits
    settings['max_concurrent_requests'] = 1

class TranslationEngine(QObject):
    LONG_PAUSE_THRESHOLD_SECONDS = 60
    MAX_REPEATED_WAITS = 5
    # --- АСИНХРОННЫЕ ИНТЕРВАЛЫ ---
    # Простые числа, близкие к 2 и 5 секундам.
    # НОК(2239, 5557) = 12,442,123 мс (совмещение через ~3.45 часа)
    RAMP_UP_INTERVAL_MS = 2239
    MONITOR_INTERVAL_MS = 5557 
    
    _worker_finished_signal = pyqtSignal(str, object)
    # Сигналы остаются, они нужны для UI
 
    def __init__(self, context_manager=None, settings_manager=None, task_manager=None, parent=None, event_bus=None):
        super().__init__(parent)
        
        app = QtWidgets.QApplication.instance()
        
        self.context_manager = context_manager
        if self.context_manager is None:
            if not hasattr(app, 'context_manager'): raise RuntimeError("ContextManager не найден.")
            self.context_manager = app.context_manager
        
        self.settings_manager = settings_manager
        if self.settings_manager is None:
            if not hasattr(app, 'settings_manager'): raise RuntimeError("SettingsManager не найден.")
            self.settings_manager = app.get_settings_manager()
        
        self.task_manager = task_manager
        if self.task_manager is None:
            if not hasattr(app, 'task_manager'): raise RuntimeError("TaskManager не был предоставлен и не найден в экземпляре QApplication.")
            self.task_manager = app.task_manager

        self.bus = event_bus
        if self.bus is None:
            if not hasattr(app, 'event_bus'): raise RuntimeError("EventBus не найден.")
            self.bus = app.event_bus

        self._uses_topic_subscription = False
        self._event_topics = (
            'cleanup_requested',
            'start_session_requested',
            'manual_stop_requested',
            'soft_stop_requested',
            'soft_stop_requested_v1_legacy',
            'temporary_limit_warning_received',
            'api_connection_healthy',
            'fatal_error',
            'task_finished',
            'assembly_finished',
            'tasks_added',
        )
        self._connect_to_bus()
        

        self.session_id = None
        self.is_cancelled = False
        self.is_session_finishing = False
        self.is_soft_stopping = False
        self.session_settings = {}
        self.is_starting = False 
        # Менеджеры, создаваемые для каждой сессии
        self.api_key_manager = None
        self.project_manager = None
        self.chunk_assembler = None
        self.key_warning_counters = {}
        
        self.last_warning_times = {} # Дебаунс для штрафов {worker_id: timestamp}
        self.pending_launches = 0    # Счетчик воркеров, ожидающих запуска в таймере
        
        self.task_statuses = {}
        self.keys_map = {}
        self.paused_keys = set()
        self.shutting_down_workers = set()
        
        self.ramp_up_timer = None
        self.session_monitor_timer = None
        
        # Пул потоков и карта для отслеживания задач
        self.executor = None 
        self.active_workers_map = {} # Теперь хранит {worker_id: future}

        # Для дебага утечек памяти
        self.session_id_for_log = None
        self._worker_finished_signal.connect(self._on_worker_finished)
    
    def _post_event(self, name: str, data: dict = None):
        event = {
            'event': name,
            'source': 'TranslationEngine',
            'session_id': self.session_id,
            'data': data or {}
        }
        if hasattr(self.bus, "emit_event"):
            self.bus.emit_event(event)
        elif hasattr(self.bus, "event_posted"):
            self.bus.event_posted.emit(event)

    def _connect_to_bus(self):
        if hasattr(self.bus, "subscribe"):
            for topic in self._event_topics:
                self.bus.subscribe(topic, self.on_event)
            self._uses_topic_subscription = True
        else:
            self.bus.event_posted.connect(self.on_event)

    def _disconnect_from_bus(self):
        try:
            if self._uses_topic_subscription and hasattr(self.bus, "unsubscribe"):
                for topic in self._event_topics:
                    self.bus.unsubscribe(topic, self.on_event)
            elif hasattr(self.bus, "event_posted"):
                self.bus.event_posted.disconnect(self.on_event)
        except (TypeError, RuntimeError, ValueError):
            pass

    @pyqtSlot(dict)
    def on_event(self, event: dict):
        try:
            self._on_event_impl(event)
        except Exception as exc:
            event_name = event.get('event', '<unknown>') if isinstance(event, dict) else '<invalid>'
            source = event.get('source', 'unknown') if isinstance(event, dict) else 'unknown'
            message = (
                f"[ENGINE-ERROR] Необработанное исключение в on_event "
                f"для события '{event_name}' от '{source}': {type(exc).__name__}: {exc}"
            )
            print(message)
            print(traceback.format_exc())
            try:
                self._post_event('log_message', {'message': message})
            except Exception:
                pass

    def _on_event_impl(self, event: dict):
        event_name = event.get('event')
        source = event.get('source', 'unknown')
        data = event.get('data', {})
    
        if event_name == 'cleanup_requested':
            self.cleanup()
            return

        event_session = event.get('session_id')
        
        # --- ЦЕНТРАЛИЗОВАННАЯ ПРОВЕРКА "ЗОМБИ" ВОРКЕРОВ ---
        if 'worker' in source:
            worker_id = source.replace('worker_', '', 1)
            worker_key = self.keys_map.get(worker_id) 

            # Если ID сессии события не совпадает с текущей активной сессией.
            if event_session != self.session_id and self.session_id is not None:
                # Отправляем команду на уничтожение воркера из старой сессии.
                self.bus.event_posted.emit({
                    'event': 'stop_session_requested', 'source': 'TranslationEngine',
                    'session_id': event_session, 'data': {'reason': "Уничтожение Зомби Воркера"}
                })
                return

            # Событие легитимно, только если воркер известен и активен.
            if not worker_key or worker_id not in self.active_workers_map:
                return

            # Если событие принесло ключ, он ДОЛЖЕН совпадать.
            worker_key_from_event = event.get('worker_key')
            if worker_key_from_event and worker_key_from_event != worker_key:
                return
        # --- КОНЕЦ ПРОВЕРКИ ---
        
        if event_name == 'start_session_requested':
            if self.is_starting or self.session_id is not None:
                self._post_event('log_message', {'message': "[ENGINE-WARN] Получена команда на старт, но сессия уже запускается или активна. Команда проигнорирована."})
                return
            self.is_starting = True
            settings = data.get('settings', {})
            if settings: self.apply_and_start_session(settings)
            else: self.is_starting = False
            return
        
        # if event_session != self.session_id and self.session_id is not None:
            # return
        
        if event_name == 'manual_stop_requested':
            self.cancel_translation(reason="Остановлено пользователем")
            return

        if event_name == 'soft_stop_requested':
            if not self.session_id or self.is_session_finishing:
                return

            if not self.is_soft_stopping:
                self.is_soft_stopping = True
                self._stop_timer('ramp_up_timer')
                self._post_event('log_message', {
                    'message': "[MANAGER] Плавная остановка активирована: новые задачи больше не выдаются, активные воркеры только завершают уже взятое."
                })

                for active_worker_id in list(self.active_workers_map.keys()):
                    self.bus.event_posted.emit({
                        'event': 'graceful_shutdown_requested',
                        'source': 'TranslationEngine',
                        'session_id': self.session_id,
                        'data': {'target_worker_id': active_worker_id}
                    })

            self._check_if_session_finished()
            return

        if event_name == 'soft_stop_requested_v1_legacy':
            if self.task_manager:
                # ВЫЗЫВАЕМ НОВЫЙ МЕТОД "ЗАМОРОЗКИ"
                held_count = self.task_manager.hold_all_pending_tasks()
                if held_count > 0:
                    self._post_event('log_message', {
                        'message': f"Плавная остановка: {held_count} задач 'заморожено'. Ожидание завершения активных воркеров..."
                    })
                # Отправляем "пульс", чтобы UI обновился и показал "замороженные" задачи
            return
    
        # Обработка "красных" и "желтых" карточек
        if event_name == 'temporary_limit_warning_received' and 'worker' in source:
            now = time.time()
            last_time = self.last_warning_times.get(worker_id, 0)
            if now - last_time < 1.0:
                return 
            self.last_warning_times[worker_id] = now

            data = event.get('data', {})
            model_id = data.get('model_id', "Unknown")
            delay = data.get('delay_seconds', 61)
            original_exception = data.get('original_exception')
            
            # --- НОВАЯ ЛОГИКА ОПРЕДЕЛЕНИЯ СТРАТЕГИИ ---
            pool_stats = self.api_key_manager.get_status_counts()
            reserve_count = pool_stats['reserve']
            active_count = pool_stats['active']
            
            # Если ключей в резерве больше, чем активных в данный момент — мы богаты, можем увольнять
            has_plenty_resource = reserve_count > active_count
            
            if has_plenty_resource:
                # Строгий режим: заменяем при 75% порога паузы или 2-й желтой карточке
                is_long_pause = delay >= self.LONG_PAUSE_THRESHOLD_SECONDS * 0.75
                has_bad_history = self.key_warning_counters.get(worker_id, 0) >= 2
            else:
                # Бережливый режим: терпим до полной минуты или накопления критических штрафов
                is_long_pause = delay >= self.LONG_PAUSE_THRESHOLD_SECONDS
                has_bad_history = self.key_warning_counters.get(worker_id, 0) >= (0.3 * self.MAX_REPEATED_WAITS)
            # ------------------------------------------

            was_exhausted = self._apply_warning_penalty(worker_id, model_id, penalty_points=1, worker_session=event_session)
            
            # Сценарий 1: Кандидат на увольнение
            if is_long_pause and has_bad_history:
                # Если ресурс есть (любой резерв > 0), инициируем ротацию
                if pool_stats['reserve'] > 0:
                    self._post_event('log_message', {
                        'message': f"[MANAGER] ❗ Ключ …{worker_key[-4:]} сбоит. Ресурс позволяет ротацию ({reserve_count} в резерве). Увольняю."
                    })
                    
                    if not was_exhausted:
                        payload = {
                            "type": "temporary_pause",
                            "exception": original_exception or Exception(f"Strategic rotation due to RPM.")
                        }
                        self._handle_fatal_error(worker_id, payload, worker_session=event_session)
                
                elif self.key_warning_counters.get(worker_id, 0) >= self.MAX_REPEATED_WAITS:
                    # Резерва нет, но превышен абсолютный лимит терпения
                    self._post_event('log_message', {
                        'message': f"[MANAGER] ❗ Ключ …{worker_key[-4:]} исчерпал лимит попыток. Увольняю, несмотря на отсутствие замены."
                    })
                    if not was_exhausted:
                        payload = {"type": "temporary_pause", "exception": original_exception}
                        self._handle_fatal_error(worker_id, payload, worker_session=event_session)
                else:
                    # Резерва нет и лимит не крайний — продолжаем работать
                    self._post_event('log_message', {
                        'message': f"[MANAGER] ⚠️ Ключ …{worker_key[-4:]} сбоит, но резерв пуст. Ключ остается в строю."
                    })

            # Сценарий 2: Просто предупреждение
            else:
                if is_long_pause:
                     self._post_event('log_message', {
                        'message': f"[MANAGER] 🟡 Ключ …{worker_key[-4:]} на паузе. (Резерв: {reserve_count}, Режим: {'Агрессивный' if has_plenty_resource else 'Щадящий'})"
                    })
        
        elif event_name == 'api_connection_healthy' and 'worker' in source:
            self._reset_key_warning_counter(worker_id)
        
        elif event_name == 'fatal_error' and 'worker' in source:
            self._handle_fatal_error(worker_id, data.get('payload'), worker_session=event_session)
        
        events_that_change_state = {
            'task_finished', 'assembly_finished', 'fatal_error', 'tasks_added'
        }
        if event_name in events_that_change_state:
            
            QtCore.QTimer.singleShot(1000, 
                                     lambda: self._check_if_session_finished())
    
    def _finalize_scheduled_launch(self, key, scheduled_session_id=None):
        """Обертка для запуска по таймеру, корректирующая счетчик ожидающих."""
        # Уменьшаем счетчик, так как этот запуск переходит из 'pending' в 'active' (внутри _launch_worker)
        if self.pending_launches > 0:
            self.pending_launches -= 1
        if scheduled_session_id and scheduled_session_id != self.session_id:
            return
        if self.is_soft_stopping:
            self._check_if_session_finished()
            return
        self._launch_worker(key)

    def _stop_timer(self, timer_name: str):
        timer = getattr(self, timer_name, None)
        if timer is None:
            return
        try:
            if timer.isActive():
                timer.stop()
        except RuntimeError:
            setattr(self, timer_name, None)
        
    def _apply_warning_penalty(self, worker_id: str, model_id: str = "Unknown", penalty_points: int = 1, worker_session=None) -> bool:
        """
        Начисляет штрафные баллы ключу.
        Если установлен лимит RPD, и текущее использование ключа > 90% от RPD,
        то ЛЮБОЕ предупреждение немедленно помечает ключ как Exhausted (красный).
        """

        if worker_id not in self.active_workers_map:
            return False # Воркер уже ушел
        if not worker_session:
            worker_session = self.session_id
        
        worker_key = self.keys_map.get(worker_id)
        if not worker_key:
            return False
        
        # --- RPD PROTECTION LOGIC ---
        rpd_limit = self.session_settings.get('rpd_limit', 0)
        
        if rpd_limit > 0:
            # Получаем текущее использование ключа
            # FIX: get_key_info находится в settings_manager, а не в api_key_manager
            
            key_info = self.settings_manager.get_key_info(worker_key) 
            if key_info:
                current_count = self.settings_manager.get_request_count(key_info, model_id)
                threshold = rpd_limit * 0.90
                
                if current_count >= threshold:
                    self._post_event('log_message', {
                        'message': f"[MANAGER-RPD] ⛔ Ключ …{worker_key[-4:]} получил ошибку при высоком расходе ({current_count}/{rpd_limit}). Моментальное списание."
                    })
                    
                    fake_exception = Exception(f"RPD Risk Threshold triggered ({current_count} >= {int(threshold)}).")
                    
                    payload = {
                        "type": "quota_exceeded",
                        "model_id": model_id,
                        "exception": fake_exception
                    }
                    event = {
                        'event': "fatal_error",
                        'source': f'worker_{worker_id}',
                        'worker_key': worker_key,
                        'session_id': worker_session,
                        'data': {'payload': payload}
                    }
                    self.bus.event_posted.emit(event)
                    return True # Ключ исчерпан
        # ----------------------------
        # ----------------------------

        current_warnings = self.key_warning_counters.get(worker_id, 0) + penalty_points
        self.key_warning_counters[worker_id] = current_warnings
        
        log_message_penalty = f"+{penalty_points} штрафных балла" if penalty_points > 1 else "желтую карточку"
        self._post_event('log_message', {
            'message': f"[MANAGER-WARN] 🟡 Ключ …{worker_key[-4:]} получил {log_message_penalty} ({current_warnings}/{self.MAX_REPEATED_WAITS})."
        })
        
        if current_warnings >= self.MAX_REPEATED_WAITS:
            self._post_event('log_message', {
                'message': f"[FATAL] ⛔ Ключ …{worker_key[-4:]} набрал максимальное количество штрафов. Считаем квоту исчерпанной."
            })
            
            fake_exception = Exception("Warning limit exceeded.")

            payload = {
                "type": "quota_exceeded",
                "model_id": model_id,
                "exception": fake_exception
            }
            
            event = {
                'event': "fatal_error",
                'source': f'worker_{worker_id}',
                'worker_key': worker_key,
                'session_id': worker_session,
                'data': {'payload': payload}
            }

            self.bus.event_posted.emit(event)
            
            return True # Да, ключ был исчерпан
            
        return False # Нет, ключ еще в игре

    def _reset_key_warning_counter(self, worker_id: str):
        """Сбрасывает счетчик предупреждений для указанного ключа."""
        if self.key_warning_counters.get(worker_id, 0) > 0:
            self.key_warning_counters[worker_id] = 0
            worker_key = self.keys_map.get(worker_id)
            if worker_key:
                self._post_event('log_message', {'message': f"[MANAGER] ✅ Ключ …{worker_key[-4:]} подтвердил работоспособность. Счетчик предупреждений сброшен."})
    

    def _handle_fatal_error(self, worker_id: str, payload: dict, worker_session=None):
        if worker_id not in self.active_workers_map or worker_id in self.shutting_down_workers:
            return
        
        if not worker_session:
            worker_session = self.session_id
        if not isinstance(payload, dict):
            payload = {}

        # Извлекаем данные из payload, который приходит от ErrorAnalyzer
        error_type = payload.get("type") if payload else "unknown"
        original_exception = payload.get("exception") if payload else None
        
        # Формируем сообщение с причиной, если она есть
        reason_text = ""
        if original_exception:
            # `str(original_exception)` вернет сообщение, с которым было создано исключение
            reason_text = f" Причина: {str(original_exception)}"


        if error_type == 'geoblock':

            self._post_event('log_message', {'message': f"[FATAL] Обнаружена блокировка по геолокации!{reason_text}"})
            self.cancel_translation(reason=f"Блокировка по геолокации.{reason_text}")
            self._post_event('geoblock_detected')
            return
        

        if error_type == 'model_not_found':
            log_message = f"[FATAL] Нет Доступа.{reason_text}"
            cancellation_reason = f"Доступ.{reason_text}"
            self._post_event('log_message', {'message': log_message})
            # Просто останавливаем сессию, без дополнительного события для UI
            self.cancel_translation(reason=cancellation_reason)
            return
        
        worker_key = self.keys_map.get(worker_id)
        if worker_key and self.api_key_manager:
            if error_type == "quota_exceeded":
                
                self.api_key_manager.mark_key_exhausted(worker_key)
                self._post_event('log_message', {'message': f"[MANAGER] Ключ …{worker_key[-4:]} помечен как исчерпанный.{reason_text}"})
            

            elif error_type == "temporary_pause":
                # Мы здесь только потому, что on_event уже проверил наличие замены.
                # Просто выполняем приказ: ставим ключ на паузу.
                delay_seconds = getattr(original_exception, 'delay_seconds', 60)
                self.api_key_manager.pause_key(worker_key)
                self._post_event('log_message', {'message': f"[MANAGER] ⏸️ Ключ …{worker_key[-4:]} на принудительной паузе на {delay_seconds} сек.{reason_text}"})
                
                QtCore.QTimer.singleShot(delay_seconds * 1000, 
                                         lambda key=worker_id: self._resume_and_relaunch(worker_key))

        
        self.shutting_down_workers.add(worker_id)
        if worker_key:
            self._post_event('log_message', {'message': f"[MANAGER] Отправка приказа на грациозное завершение воркеру …{worker_id[-4:]} (ключ: …{worker_key[-4:]})."})
        
        
        event = {
            'event': 'graceful_shutdown_requested',
            'source': 'TranslationEngine',
            'session_id': worker_session,
            'data': {'target_worker_id': worker_id}
        }
        self.bus.event_posted.emit(event)

        if self._try_launch_replacement():
            self._post_event('worker_rotated', {'old_worker_id': worker_id})
        self._check_if_session_finished()
    
    def _resume_and_relaunch(self, key_to_resume):
        """
        Снимает ключ с паузы и немедленно пытается запустить для него воркера,
        если есть свободные слоты.
        """
        if self.is_cancelled or not self.session_id:
            return

        self._post_event('log_message', {'message': f"[MANAGER] ⏰ Пауза для ключа …{key_to_resume[-4:]} истекла. Ключ возвращен в работу."})
        if self.api_key_manager:
            self.api_key_manager.resume_key(key_to_resume)
        
        # После "разбана" сразу же проверяем, не нужно ли запустить нового воркера
        # (например, если сессия еще идет, но ключей не хватало)
        self._try_launch_replacement()



    def apply_and_start_session(self, settings: dict):
        if self.session_id:
            self._post_event('log_message', {'message': "[ERROR] Попытка запустить новую сессию, когда предыдущая еще активна."})
            return
        
        full_glossary_dict = settings.get('full_glossary_data', {})
        use_jieba_for_glossary = settings.get('use_jieba', False)
        segment_cjk_text = settings.get('segment_cjk_text', False)

        normalize_sequential_parallel_settings(
            settings,
            lambda message: self._post_event('log_message', {'message': message})
        )

        if full_glossary_dict and self.context_manager.chinese_processor and (use_jieba_for_glossary or segment_cjk_text):
            self._post_event('log_message', {'message': "[JIEBA] Обучение Jieba на глоссарии сессии…"})
            self.context_manager.chinese_processor.add_custom_words(full_glossary_dict)
        else:
            self._post_event('log_message', {'message': "[JIEBA] Обучение Jieba пропущено, так как соответствующие опции отключены."})
        
        self.context_manager.update_settings(settings)
        self._post_event('log_message', {'message': "[MANAGER] Контекст сессии (глоссарий) обновлен."})
        
        self._terminate_all_workers()
        self.session_settings = settings
        
        if self.is_managed_mode():
            self._post_event('log_message', {'message': "[ENGINE] Сессия запущена в УПРАВЛЯЕМОМ режиме."})

        self.session_id = str(uuid.uuid4())
        self.session_id_for_log = self.session_id
        self.summary_shown_for_session = False
        self.is_cancelled = False
        self.is_session_finishing = False
        self.is_soft_stopping = False
        self.task_statuses.clear()
        self.paused_keys.clear()
        self.shutting_down_workers.clear()
        self.key_warning_counters.clear()
        self.pending_launches = 0
        self.keys_map.clear()
        self.last_warning_times.clear()
        self.api_key_manager = None
        self.project_manager = None
        self.chunk_assembler = None
        
        self._post_event('log_message', {'message': f"▶▶▶ Начало новой сессии: {self.session_id[:8]}"})
        
        try:
            num_instances = int(settings.get('num_instances', 1))
        except (TypeError, ValueError):
            self._end_session("Критическая ошибка: некорректное количество воркеров.")
            return
        settings['num_instances'] = num_instances
        total_session_keys = settings.get('api_keys', [])
        if num_instances <= 0:
            self._end_session("Критическая ошибка: количество воркеров должно быть больше нуля.")
            return
        if not total_session_keys:
            self._end_session("Нет доступных API ключей для запуска.")
            return
        max_pool_size = len(total_session_keys)
        self.executor = ThreadPoolExecutor(max_workers=max_pool_size, thread_name_prefix='WorkerThread')
        
        # --- Блок инициализации сессии (с изменениями) ---
        
        # 0. Загружаем настройки прокси
        proxy_settings = self.settings_manager.load_proxy_settings()
        
        # ВАЖНО: Сохраняем их в объект настроек сессии!
        settings['proxy_settings'] = proxy_settings # <-- ДОБАВЛЯЕМ ЭТУ СТРОКУ
        
        # 1. Проверяем, существует ли TaskManager и есть ли в нем задачи
        settings['model_id'] = settings.get('model_config', {}).get('id')

        if not self.task_manager or not self.task_manager.has_pending_tasks():
            self._end_session("Нет задач для перевода.")
            return

        # 2. Определяем тип сессии, заглядывая в ПЕРВУЮ задачу в очереди
        # --- ИЗМЕНЕНИЕ: Логика получения первой задачи адаптирована под SQLite ---
        first_task_payload = self.task_manager.get_first_pending_task_payload()
        first_task_type = first_task_payload[0] if first_task_payload else None
        
        # 3. Инициализируем нужные компоненты


        self.project_manager = None
        
        if first_task_type == 'glossary_batch_task':
            merge_mode = settings.get('glossary_merge_mode', 'supplement')
            self._post_event('log_message', {'message': f"[MANAGER] Активирован режим генерации. Слияние: {merge_mode}."})
        else:
            output_folder = settings.get('output_folder')
            if not output_folder and first_task_type != 'raw_text_translation':
                self._end_session("Критическая ошибка: не указана папка для вывода.")
                return
            self.project_manager = settings.get('project_manager')
        
        # --- КОНЕЦ БЛОКА ИНИЦИАЛИЗАЦИИ ---
        
        if self.task_manager:
            self.task_manager.session_id = self.session_id
        
        self.api_key_manager = ApiKeyManager(total_session_keys)
        
        if self.project_manager and output_folder:
            self.chunk_assembler = ChunkAssembler(output_folder, self.project_manager, settings)
        else:
            self.chunk_assembler = None
        total_tasks_for_session = len(self.task_manager.get_all_pending_tasks())
        model_id = settings.get('model_id')
        self._post_event('session_started', {
            'session_id': self.session_id,
            'model_id': model_id,
            'total_tasks': total_tasks_for_session,
            'settings': {
                'model_config': settings.get('model_config', {}),
                'model_id': model_id
            }
        })
        
        self.model_id = model_id
        self.is_starting = False
        self._start_timers()
        
        num_to_start = min(num_instances, len(self.api_key_manager.api_keys))
        if num_to_start == 0:
            self._end_session("Нет доступных API ключей для запуска.")
            return
        
        if num_to_start > 1:
            self._post_event('log_message', {'message': f"[RAMP-UP] Плавный запуск {num_to_start} воркеров…"})
        if self.bus:
            self.bus.set_data("current_active_session", self.session_id)
        

    def is_managed_mode(self):
        if self.bus and hasattr(self.bus, '_data_store'):
            # Ищем любой ключ, начинающийся с 'managed_session_active_'
            for key in self.bus._data_store.keys():
                if key.startswith('managed_session_active_') and self.bus.get_data(key) is True:
                    return True
        return False
    
    
    def cancel_translation(self, reason: str = "Отменено пользователем"):
        if self.session_id:
            self._end_session(reason)

    def _end_session(self, reason: str):
        # 1. Очистка глобального трекера сессии
        self.bus.pop_data("current_active_session", None)
        
        # 2. Принудительная зачистка флагов Оркестратора.
        # Это гарантирует, что при любом выходе (ошибка, стоп, финиш)
        # система выйдет из управляемого режима и воркеры перестанут ждать.
        if self.bus and hasattr(self.bus, '_data_store'):
             # Создаем список ключей для удаления (чтобы не менять словарь во время итерации)
             orchestrator_keys = [k for k in self.bus._data_store.keys() if k.startswith('managed_session_active_')]
             for k in orchestrator_keys:
                 self.bus.pop_data(k, None)
                 
        if not self.session_id or self.is_session_finishing:
            return
        
        self.is_session_finishing = True
        self.is_cancelled = True 
        self.is_soft_stopping = False
        
        self._post_event('stop_session_requested', {'reason': reason}) 
        self._stop_timers()
        
        self._terminate_all_workers()
        
        self._end_session_event(reason, self.session_id)
        if not self.summary_shown_for_session:
            self.show_summary_data()

        if self.context_manager and self.context_manager.chinese_processor:
            self.context_manager.chinese_processor.reset()
        
        # 1. Сначала отправляем сигнал о завершении, пока ID еще валиден
        
        
        self.session_id = None
        self.is_starting = False # <-- Сбрасываем и этот флаг тоже
        
    def _end_session_event(self, reason: str, session_id_event=None):
        self._post_event('session_finished', {'reason': reason, "session_id_log": self.session_id})
    
    def _launch_next_from_ramp_up(self):
        if self.is_soft_stopping:
            self._stop_timer('ramp_up_timer')
            return
        # Проверяем, есть ли вообще задачи, прежде чем что-то запускать
        if not self.task_manager or not self.task_manager.has_pending_tasks():
            self._stop_timer('ramp_up_timer')
            self._post_event('log_message', {'message': "[RAMP-UP] Задачи закончились. Плавный запуск остановлен."})
            return

        num_instances_limit = self.session_settings.get('num_instances', 1)
        
        if self.is_cancelled or len(self.active_workers_map) >= num_instances_limit or self.is_session_finishing:
            self._stop_timer('ramp_up_timer')
            if not self.is_cancelled and not self.is_session_finishing:
                self._post_event('log_message', {'message': "[RAMP-UP] Все запланированные воркеры запущены."})
            return
        
        # Извлекаем реальные API-ключи по ID активных воркеров из keys_map.
        current_active_keys = [
            self.keys_map.get(w_id)
            for w_id in self.active_workers_map 
            if w_id in self.keys_map
        ]

        if not self.api_key_manager:
            self._stop_timer('ramp_up_timer')
            return

        self.api_key_manager.update_active(current_active_keys)     
        
        key_to_launch = self.api_key_manager.get_next_available_key()
        if key_to_launch:
            self._launch_worker(key_to_launch)
        else:
            self._stop_timer('ramp_up_timer') # Ключи кончились
            self._post_event('log_message', {'message': "[RAMP-UP] Доступные ключи закончились."})


    def _launch_worker(self, api_key: str):
        current_worker_id = self.keys_map.get(api_key)
        
        if self.is_cancelled or self.is_session_finishing or self.is_soft_stopping or (current_worker_id and current_worker_id in self.active_workers_map):
            return
        
        worker_params = self.session_settings.copy()
        # Получаем полный список ключей сессии для проверки коллизий
        all_session_keys = self.session_settings.get('api_keys', [])
        
        # Генерируем UUID, которого нет ни в карте (активные ID/ключи), 
        # ни в исходном списке ключей (даже если они еще не в карте)
        while True:
            candidate_uuid = str(uuid.uuid4())
            if candidate_uuid not in self.keys_map and candidate_uuid not in all_session_keys:
                uuid_worker = candidate_uuid
                break
        self.keys_map.update({uuid_worker: api_key})
        self.keys_map.update({api_key: uuid_worker})
        
        self.api_key_manager.update_map(self.keys_map)

        worker_params.update({
            'api_provider_name': self.session_settings.get('provider'),
            'client_map': {api_key: type('obj', (object,), {'api_key': api_key})()},
            'worker_id': uuid_worker,
            'api_key': api_key,
            'session_id': self.session_id,
            'api_key_manager': self.api_key_manager,
            'task_manager': self.task_manager,
            'context_manager': self.context_manager,
            'chunk_assembler': self.chunk_assembler,
            'settings_manager': self.settings_manager,
            'project_manager': self.project_manager,
        })
        
        worker = UniversalWorker(**worker_params)

        future = self.executor.submit(worker.run)
        self.active_workers_map[uuid_worker] = future
        
        # --- ГЛАВНОЕ ИЗМЕНЕНИЕ: Теперь колбэк ИСПУСКАЕТ СИГНАЛ ---
        future.add_done_callback(
            lambda f, key=uuid_worker: self._worker_finished_signal.emit(key, f)
        )

        self._post_event('log_message', {'message': f"[MANAGER] Воркер …{uuid_worker[-4:]} для ключа …{api_key[-4:]} запущен в пуле."})

    @pyqtSlot(str, object)
    def _on_worker_finished(self, worker_id, future):
        try:
            self._on_worker_finished_impl(worker_id, future)
        except Exception as exc:
            message = (
                f"[ENGINE-ERROR] Необработанное исключение в _on_worker_finished "
                f"для воркера …{str(worker_id)[-4:]}: {type(exc).__name__}: {exc}"
            )
            print(message)
            print(traceback.format_exc())
            try:
                self._post_event('log_message', {'message': message})
            except Exception:
                pass

    def _on_worker_finished_impl(self, worker_id, future):
        """
        СЛОТ, ВЫПОЛНЯЕМЫЙ В ПОТОКЕ TranslationEngine.
        Безопасно обрабатывает завершение работы воркера.
        """
        # 1. Пытаемся спасти задачи
        if self.task_manager:
            try:
                self.task_manager.rescue_task_by_worker_id(worker_id)
            except Exception as e:
                self._post_event('log_message', {'message': f"[MANAGER_ERROR] Не удалось спасти задачи воркера …{worker_id[-4:]}: {e}"})


        try:
            future.result() 
        except Exception as e:
            pass # Лог уже есть выше

        # ЗАЩИТА ОТ ЗОМБИ:
        # Проверяем, является ли завершившийся future ТЕКУЩИМ активным future для этого ключа.
        # Если мы уже запустили замену (см. _try_launch_replacement), то в active_workers_map 
        # лежит НОВЫЙ future. Старый (этот) трогать нельзя.
        
        current_active_future = self.active_workers_map.get(worker_id)
        worker_key = self.keys_map.get(worker_id)

        if current_active_future == future:
            # Это штатное завершение актуального воркера
            self._post_event('log_message', {'message': f"[MANAGER] Воркер …{worker_id[-4:]} завершил работу."})
            del self.active_workers_map[worker_id]
            
            if worker_key:
                # Освобождаем ключ
                if self.api_key_manager:
                    self.api_key_manager.release_key(worker_key)
        else:
            # Это завершился "зомби" (старый воркер), которого мы уже заменили
            self._post_event('log_message', {'message': f"[MANAGER] 💀 Поток воркера …{worker_id[-4:]} окончательно остановился."})
            # Доп. защита: если зомби почему-то застрял в списке активных — убиваем
            if worker_id in self.active_workers_map:
                del self.active_workers_map[worker_id]
            # Ключ НЕ освобождаем, так как новый воркер возможно его использует!
                
        # --- ОБЩАЯ ЧИСТКА ПАМЯТИ (KEYS MAP) ---
        if worker_key:
            # Если запись ключа ссылается ИМЕННО на этого (уже мертвого) воркера — удаляем её.
            # Если там уже ID нового воркера (после ротации) — не трогаем.
            if self.keys_map.get(worker_key) == worker_id:
                del self.keys_map[worker_key]
            
            # Запись самого ID воркера удаляем всегда (он мертв)
            if worker_id in self.keys_map:
                del self.keys_map[worker_id]
            if self.api_key_manager:
                self.api_key_manager.update_map(self.keys_map)
                
        self.shutting_down_workers.discard(worker_id)

        # Проверка сессии
        if not self._try_launch_replacement():
            self._check_if_session_finished()
    
    def _try_launch_replacement(self):
        if self.is_cancelled or self.is_session_finishing or self.is_soft_stopping:
            self._stop_timer('session_monitor_timer')
            return False

        if not self.task_manager or not self.task_manager.has_pending_tasks() or not self.api_key_manager:
            return False
        
        num_instances_limit = self.session_settings.get('num_instances', 1)
        
        # FIX: Учитываем не только живых, но и тех, кто уже "на подходе" (в таймере)
        num_truly_active = (len(self.active_workers_map) - len(self.shutting_down_workers)) + self.pending_launches
        
        # Если у нас уже полный комплект (активных + ожидающих запуска), ничего не делаем
        if num_truly_active >= num_instances_limit:
            return False
            
        # 1. Запрашиваем ключ.
        key_to_launch = self.api_key_manager.get_next_available_key()
        
        if key_to_launch:
            # 2. ПРОВЕРКА НА КОНФЛИКТ (Ротация на том же ключе)
            if self.keys_map.get(key_to_launch) and self.keys_map.get(key_to_launch) in self.active_workers_map:
                if self.keys_map.get(key_to_launch) in self.shutting_down_workers:
                    self._post_event('log_message', {'message': f"[MANAGER] ♻️ Ротация: Запускаю свежую замену для ключа …{key_to_launch[-4:]}, пока старый воркер завершается."})
                    
                    # ХИТРОСТЬ: Удаляем сразу, запускаем сразу. Таймер не нужен.
                    old_future = self.active_workers_map.pop(self.keys_map.get(key_to_launch))
                    self.shutting_down_workers.discard(self.keys_map.get(key_to_launch))
                    self._launch_worker(key_to_launch)
                    return True
                else:
                    return False
            
            # 3. Обычный запуск (ключ свободен)
            self._post_event('log_message', {'message': f"[MANAGER] 🚀 Запуск дополнительного воркера для ключа: …{key_to_launch[-4:]}"})
            
            # FIX: Инкрементируем счетчик ожидающих, чтобы следующий вызов (например, от on_worker_finished)
            # знал, что слот уже занят этим запланированным запуском.
            self.pending_launches += 1
            
            QtCore.QTimer.singleShot(self.RAMP_UP_INTERVAL_MS, 
                                     lambda key=key_to_launch, session_id=self.session_id: self._finalize_scheduled_launch(key, session_id))
            return True
            
        return False

    def _terminate_all_workers(self):
        if not self.executor:
            return
            
        self._post_event('log_message', {'message': "[MANAGER] Остановка пула потоков… Ожидание завершения активных задач..."})
        
        # --- ГЛАВНОЕ ИЗМЕНЕНИЕ: wait=True ---
        # Теперь этот вызов будет БЛОКИРУЮЩИМ. Он не вернет управление,
        # пока все запущенные задачи не завершатся (успешно или с ошибкой).
        # Это безопасно, так как TranslationEngine работает в своем собственном потоке (QThread)
        # и не заморозит графический интерфейс.
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # Fallback для версий Python < 3.9, где нет cancel_futures
            self.executor.shutdown(wait=True)
        self.executor = None
        self.active_workers_map.clear()
        self._post_event('log_message', {'message': "[MANAGER] Пул потоков полностью остановлен."})
        self.bus.pop_data("current_active_session", None)

    def __del__(self):
        session_id = getattr(self, 'session_id_for_log', 'unknown')
        if session_id:
            print(f"[MANAGER LIFECYCLE] Объект TranslationEngine для сессии …{session_id[:8]} УНИЧТОЖЕН.")
        
    def _internal_log(self, session_id: str, message: str):
        if self.session_id and session_id == self.session_id:
            self._post_event('log_message', {'message': message})

    def _check_if_session_finished(self):
        """
        Проверяет условия завершения сессии.
        Версия 13.0 ("Доверие Менеджеру"):
        Полностью полагается на логику task_manager.is_finished().
        """
        if self.is_session_finishing or not self.task_manager:
            return

        if not self.api_key_manager:
            return

        self.api_key_manager.update_map(self.keys_map)
        if self.is_soft_stopping:
            if not self.active_workers_map and self.pending_launches == 0:
                self._post_event('log_message', {
                    'message': "[MANAGER] Плавная остановка завершена: активные задачи дочищены, новые задачи остались в очереди."
                })
                self._end_session("Плавная остановка завершена")
            return
        
        # 1. ГЛАВНЫЙ ВОПРОС: Мы закончили?
        # TaskManager сам проверил и базу, и флаги оркестратора.
        if self.task_manager.is_finished():
            self._post_event('log_message', {'message': "[MANAGER] Работа завершена (Задачи выполнены / Флаг снят)."})
            self.show_summary_data()
            self._end_session("Сессия успешно завершена")
            return

        # 2. АВАРИЙНАЯ ПРОВЕРКА (Если мы НЕ закончили, но воркеров нет)
        no_workers_active = not self.active_workers_map
        if no_workers_active:
            # Если это управляемый режим — это норма (ждем оркестратора),
            # но is_finished уже вернул False (значит флаг стоит).
            # Мы просто проверяем, не "тупик" ли это (нет ключей).
            
            if self.is_managed_mode():
                 # В управляемом режиме отсутствие воркеров при наличии флага - норма.
                 # Проверяем только полную безысходность (нет ключей).
                 if not self.api_key_manager.has_non_exhausted_keys():
                     self._post_event('log_message', {'message': "[MANAGER] ТУПИК: Оркестратор активен, но все ключи исчерпаны."})
                     self.show_summary_data()
                     self._end_session("Все API ключи исчерпаны")
                 return

            # В обычном режиме отсутствие воркеров при is_finished=False — это проблема.
            if not self.api_key_manager.has_non_exhausted_keys():
                self._post_event('log_message', {'message': "[MANAGER] РАБОТА ОСТАНОВЛЕНА: задачи есть, но все ключи исчерпаны."})
                self.show_summary_data()
                self._end_session("Все API ключи исчерпаны")
            else:
                self._try_launch_replacement()
        

    
    def show_summary_data(self):
        if hasattr(self, 'summary_shown_for_session') and self.summary_shown_for_session:
            return

        self.summary_shown_for_session = True
        
        filtered_count = sum(1 for status in self.task_statuses.values() if status == 'filtered')
        
        if filtered_count == 0:
            return
        
        self._post_event('log_message', {'message': "---SEPARATOR---"})
        self._post_event('log_message', {'message': "📋 СВОДКА ПО ЗАБЛОКИРОВАННЫМ ГЛАВАМ"})
        self._post_event('log_message', {'message': f"Всего заблокировано фильтрами: {filtered_count} задач."})
        self._post_event('log_message', {'message': "Чтобы перевести их, используйте кнопку \"Оставить только 'Фильтр'\", измените модель/провайдера и запустите сессию снова."})
        self._post_event('log_message', {'message': "---SEPARATOR---"})
        
    # --- НАЧАЛО НОВОГО КОДА: Слоты для управления таймерами ---
    @pyqtSlot()
    def _start_timers(self):
        """Создает и запускает таймеры в своем собственном потоке."""
        self._stop_timer('ramp_up_timer')
        self._stop_timer('session_monitor_timer')

        if self.ramp_up_timer is None:
            self.ramp_up_timer = QtCore.QTimer(self)
            self.ramp_up_timer.timeout.connect(self._launch_next_from_ramp_up)
        
        if self.session_monitor_timer is None:
            self.session_monitor_timer = QtCore.QTimer(self)
            self.session_monitor_timer.timeout.connect(self._check_if_session_finished)

        self.session_monitor_timer.start(self.MONITOR_INTERVAL_MS)
        self.ramp_up_timer.start(self.RAMP_UP_INTERVAL_MS)
        self._post_event('log_message', {'message': "[MANAGER] Таймеры сессии активированы."})

    @pyqtSlot()
    def _stop_timers(self):
        """Останавливает таймеры в своем собственном потоке."""
        self._stop_timer('ramp_up_timer')
        self._stop_timer('session_monitor_timer')
        if not self.is_cancelled and not self.is_session_finishing:
            self._post_event('log_message', {'message': "[MANAGER] Таймеры сессии деактивированы."})
    
    @pyqtSlot()
    def log_thread_identity(self):
        """Выводит ID потока и регистрирует его как VIP."""
        import threading
        # Импортируем os_patch, чтобы добраться до класса
        import os_patch 
        
        ident = threading.get_ident()
        print(f"\n[SYSTEM] ENGINE THREAD ID: {ident}\n")
        
        # Регистрируем текущий поток (поток движка) как бессмертный
        os_patch.PatientLock.register_vip_thread(ident)
    
    @pyqtSlot()    
    def cleanup(self):
        self._stop_timers()
        self._disconnect_from_bus()

        self.is_cancelled = True # Финальный флаг для всех
        self._terminate_all_workers()
