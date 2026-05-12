import threading
import asyncio
import aiohttp
import os
import re
import contextvars
import ssl
import time
import sys
from collections import Counter
from PyQt6.QtWidgets import QApplication
from ..utils.async_helpers import run_sync
from ..utils.debug_logger import create_operation_trace
from .errors import (
    OperationCancelledError, ContentFilterError, RateLimitExceededError, LocationBlockedError, SuccessSignal,
    ModelNotFoundError, ValidationFailedError, NetworkError, PartialGenerationError, TemporaryRateLimitError, GracefulShutdownInterrupt
)

_thread_local = threading.local()
_current_debug_trace = contextvars.ContextVar("current_debug_trace", default=None)

try:
    import requests
    from requests.exceptions import RequestException as RequestsError
except ImportError:
    requests = None
    class RequestsError(Exception): pass

try:
    import socks
    from aiohttp_socks import ProxyConnector, ProxyType
    PROXY_ERRORS = (socks.ProxyError, socks.GeneralProxyError, socks.ProxyConnectionError)
except (ImportError, AttributeError):
    socks = None
    ProxyConnector = None
    ProxyType = None
    PROXY_ERRORS = ()

try:
    import certifi
except ImportError:
    certifi = None


def _create_ssl_context():
    """Create a reliable SSL context for aiohttp across python.org/macOS builds."""
    cafile = os.environ.get("SSL_CERT_FILE") or None
    capath = os.environ.get("SSL_CERT_DIR") or None
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath)

    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception as exc:
            print(f"[API WARN] Не удалось загрузить certifi CA bundle: {exc}")

    return ssl.create_default_context()

def get_worker_loop():
    """Получает или создает event loop для текущего потока воркера."""
    if not hasattr(_thread_local, "loop") or _thread_local.loop.is_closed():
        if sys.platform == "win32":
            proactor_loop_class = getattr(asyncio, "ProactorEventLoop", None)
            if proactor_loop_class is not None:
                _thread_local.loop = proactor_loop_class()
            else:
                _thread_local.loop = asyncio.new_event_loop()
        else:
            _thread_local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_thread_local.loop)
    return _thread_local.loop

class BaseApiHandler:
    """
    Базовый класс-стратегия с умной гибридной логикой.
    Версия 5.0: Исправлена диспетчеризация асинхронных вызовов.
    """
    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.is_async_native = self.worker.provider_config.get("is_async", False)
        self.proxy_settings = None

    def _proactive_session_init(self):
        api_timeout = self.worker.provider_config.get("base_timeout", 600)
        loop = get_worker_loop()
        loop.run_until_complete(self._get_or_create_session_internal(api_timeout))
        
    def setup_client(self, client_override=None, proxy_settings=None):
        """Базовая настройка."""
        self.proxy_settings = proxy_settings
        return True

    def _get_proxy_signature(self):
        """Returns a stable signature for the active proxy configuration."""
        if not self.proxy_settings or not self.proxy_settings.get('enabled'):
            return None

        return (
            str(self.proxy_settings.get('type', 'SOCKS5')).upper(),
            str(self.proxy_settings.get('host') or ''),
            str(self.proxy_settings.get('port') or ''),
            str(self.proxy_settings.get('user') or ''),
            str(self.proxy_settings.get('pass') or ''),
        )

    async def _get_or_create_session_internal(self, api_timeout=600):
        """[Внутренний] Лениво создает сессию."""
        desired_proxy_signature = self._get_proxy_signature()
        existing_session = getattr(_thread_local, "session", None)
        existing_proxy_signature = getattr(_thread_local, "session_proxy_signature", None)
        existing_timeout = getattr(_thread_local, "session_timeout", None)

        if existing_session and not existing_session.closed:
            if existing_proxy_signature == desired_proxy_signature and existing_timeout == api_timeout:
                return existing_session

            await existing_session.close()
            delattr(_thread_local, "session")
            if hasattr(_thread_local, "session_proxy_signature"):
                delattr(_thread_local, "session_proxy_signature")
            if hasattr(_thread_local, "session_timeout"):
                delattr(_thread_local, "session_timeout")
        
        ssl_context = _create_ssl_context()
        connector = None
        if self.proxy_settings and self.proxy_settings.get('enabled'):
            try:
                host = self.proxy_settings.get('host')
                port = self.proxy_settings.get('port')
                p_type = self.proxy_settings.get('type', 'SOCKS5').lower()
                user = self.proxy_settings.get('user')
                pwd = self.proxy_settings.get('pass')
                
                if host and port and ProxyConnector is not None:
                    auth = f"{user}:{pwd}@" if user and pwd else ""
                    url = f"{p_type}://{auth}{host}:{port}"
                    connector = ProxyConnector.from_url(url, rdns=True, ssl=ssl_context)
                elif host and port:
                    print("[API ERROR] Прокси включен, но aiohttp_socks недоступен.")
            except Exception as e:
                print(f"[API ERROR] Не удалось создать прокси-коннектор: {e}")

        if connector is None:
            connector = aiohttp.TCPConnector(ssl=ssl_context)

        timeout = aiohttp.ClientTimeout(total=api_timeout)
        _thread_local.session = aiohttp.ClientSession(
            loop=get_worker_loop(),
            timeout=timeout,
            connector=connector 
        )
        _thread_local.session_proxy_signature = desired_proxy_signature
        _thread_local.session_timeout = api_timeout
        return _thread_local.session

    async def _close_thread_session_internal(self):
        """[Внутренний] Закрывает сессию для текущего потока."""
        if hasattr(_thread_local, "session"):
            session = getattr(_thread_local, "session")
            if session and not session.closed:
                await session.close()
            delattr(_thread_local, "session")
        if hasattr(_thread_local, "session_proxy_signature"):
            delattr(_thread_local, "session_proxy_signature")
        if hasattr(_thread_local, "session_timeout"):
            delattr(_thread_local, "session_timeout")

    def _create_debug_trace(self, log_prefix):
        if not getattr(self.worker, "debug_logging_enabled", False):
            return None

        context_getter = getattr(self.worker, "get_debug_operation_context", None)
        operation_context = context_getter() if callable(context_getter) else {}
        trace = create_operation_trace(
            worker=self.worker,
            log_prefix=log_prefix,
            operation_context=operation_context,
            raw_filters=getattr(self.worker, "debug_operation_filters", None),
            max_total_mb=getattr(self.worker, "debug_max_log_mb", 128),
        )

        if trace and trace.session_announcement_needed:
            self.worker._post_event('log_message', {
                'message': f"[DEBUG] Логи отладки сессии активны: {trace.session_dir.name}",
                'file_path': str(trace.session_dir),
                'file_label': 'Открыть папку debug',
            })
        return trace

    def _debug_record_request(self, raw_request=None, *, attempt=1, extra=None):
        trace = _current_debug_trace.get()
        if trace:
            trace.write_event("request", attempt=attempt, raw_request=raw_request, extra=extra)

    def _has_debug_trace(self) -> bool:
        return _current_debug_trace.get() is not None

    def _debug_record_response(self, raw_response=None, *, attempt=1, status=None, extra=None):
        trace = _current_debug_trace.get()
        if trace:
            trace.write_event(
                "response",
                attempt=attempt,
                raw_response=raw_response,
                status=status,
                extra=extra,
            )

    def _debug_record_error(self, error, *, attempt=1, status=None, raw_response=None, extra=None):
        trace = _current_debug_trace.get()
        if trace:
            trace.write_event(
                "error",
                attempt=attempt,
                raw_response=raw_response,
                status=status or self._debug_status_from_exception(error),
                error=error,
                extra=extra,
            )

    def _debug_status_from_exception(self, error: Exception) -> str:
        if isinstance(error, OperationCancelledError):
            return "cancelled"
        if isinstance(error, ContentFilterError):
            return "content_filter"
        if isinstance(error, PartialGenerationError):
            return "partial_generation"
        if isinstance(error, TemporaryRateLimitError):
            return "temporary_rate_limit"
        if isinstance(error, RateLimitExceededError):
            return "rate_limit"
        if isinstance(error, LocationBlockedError):
            return "location_blocked"
        if isinstance(error, ModelNotFoundError):
            return "model_not_found"
        if isinstance(error, ValidationFailedError):
            return "validation_failed"
        if isinstance(error, NetworkError):
            return "network_error"
        return type(error).__name__.lower()

    def _temperature_payload_value(self):
        if not getattr(self.worker, "temperature_override_enabled", True):
            return None
        value = getattr(self.worker, "temperature", None)
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _finalize_debug_trace(self, trace, *, started_at, status, error=None):
        if not trace:
            return

        duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        trace.finalize(status=status, duration_ms=duration_ms, error=error)

        if error is not None:
            self.worker._post_event('log_message', {
                'message': f"[DEBUG] Сохранен файл отладки для ошибки: {type(error).__name__}",
                'file_path': str(trace.operation_path),
                'file_label': 'Открыть debug-лог',
            })

    
    def _force_session_reset(self):
        """
        [Внутренний] Принудительно удаляет сессию из thread_local.
        Используется при критических ошибках соединения (ServerDisconnected и т.д.),
        чтобы следующий запрос гарантированно создал чистое подключение.
        """
        if hasattr(_thread_local, "session"):
            session = getattr(_thread_local, "session")
            # Пытаемся закрыть корректно, но не блокируемся, если это невозможно синхронно
            if session and not session.closed:
                try:
                    # Создаем задачу на закрытие в текущем лупе, не дожидаясь её
                    loop = get_worker_loop()
                    if loop.is_running():
                        loop.create_task(session.close())
                except Exception:
                    pass # Игнорируем ошибки закрытия, так как мы все равно удаляем ссылку
            
            delattr(_thread_local, "session")
        if hasattr(_thread_local, "session_proxy_signature"):
            delattr(_thread_local, "session_proxy_signature")
        if hasattr(_thread_local, "session_timeout"):
            delattr(_thread_local, "session_timeout")
    
    
    
    async def execute_api_call(self, prompt, log_prefix, allow_incomplete=False, debug=False, use_stream=True, max_output_tokens=None):
        self.worker.settings_manager.increment_request_count(self.worker.api_key, self.worker.model_id)
        trace = self._create_debug_trace(log_prefix)
        trace_token = _current_debug_trace.set(trace)
        started_at = time.perf_counter()

        try:
            try:
                if self.is_async_native:
                    result = await self._async_executor(prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens)
                else:
                    result = await self._sync_executor_wrapper(prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens)
            except asyncio.CancelledError as exc:
                if self.worker.is_cancelled:
                    raise OperationCancelledError("Операция отменена системой (asyncio.CancelledError)") from exc

                error_msg = "Запрос прерван (CancelledError). Вероятная причина: таймаут DNS или сброс соединения."
                self._force_session_reset()
                raise NetworkError(error_msg, delay_seconds=10) from exc
            except Exception as exc:
                self._process_exception_and_counters(exc)
                raise

            self._finalize_debug_trace(trace, started_at=started_at, status="success")
            return result
        except Exception as exc:
            self._finalize_debug_trace(
                trace,
                started_at=started_at,
                status=self._debug_status_from_exception(exc),
                error=exc,
            )
            raise
        finally:
            _current_debug_trace.reset(trace_token)

    async def _async_executor(self, prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens):
        """
        Обертка для асинхронных вызовов.
        """
        # 1. Создаем корутину вызова API. 
        # ВАЖНО: Это только объект, код внутри call_api еще не выполняется.
        api_coroutine = self.call_api(
            prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens
        )
        
        api_timeout = self.worker.provider_config.get("base_timeout", 600)

        # 2. Оборачиваем корутину в wait_for (тоже корутина)
        api_task_with_timeout = asyncio.wait_for(api_coroutine, timeout=api_timeout)

        # 3. Создаем задачи для цикла событий. Вот ТУТ они начинают выполняться.
        checker_task = asyncio.create_task(self._cancellation_checker())
        api_task = asyncio.create_task(api_task_with_timeout)
        
        try:
            done, pending = await asyncio.wait({api_task, checker_task}, return_when=asyncio.FIRST_COMPLETED)
            
            if checker_task in done:
                # Если сработала отмена
                api_task.cancel()
                # Обязательно дожидаемся отмены, чтобы избежать warning'ов
                try:
                    await api_task
                except asyncio.CancelledError:
                    pass
                raise OperationCancelledError("Отмена обнаружена во время ожидания API")
            
            if api_task in done:
                # Если API ответило (или упало с ошибкой)
                checker_task.cancel()
                return await api_task
                
        except asyncio.TimeoutError:
            checker_task.cancel()
            api_task.cancel() # Отменяем зависший запрос
            raise NetworkError(f"Глобальный таймаут API ({api_timeout}с) превышен.", delay_seconds=30)
        except Exception as e:
            # Страховка на случай непредвиденных ошибок в asyncio.wait
            checker_task.cancel()
            if not api_task.done():
                api_task.cancel()
            raise e

    async def _sync_executor_wrapper(self, prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens):
        """Обертка для СИНХРОННЫХ вызовов (через run_sync)."""
        api_timeout = self.worker.provider_config.get("base_timeout", 600)
        
        api_coro = run_sync(
            self.call_api,
            prompt, log_prefix, allow_incomplete, use_stream, debug, max_output_tokens,
            forget=False,
            timeout=api_timeout
        )
        
        checker_task = asyncio.create_task(self._cancellation_checker())
        api_task = asyncio.create_task(api_coro)
        
        try:
            done, pending = await asyncio.wait({api_task, checker_task}, return_when=asyncio.FIRST_COMPLETED)
            if checker_task in done:
                api_task.cancel()
                raise OperationCancelledError("Отмена обнаружена во время ожидания API")
            if api_task in done:
                checker_task.cancel()
                return await api_task
        except asyncio.TimeoutError:
            checker_task.cancel()
            raise NetworkError(f"Глобальный таймаут API ({api_timeout}с) превышен.")

    async def _cancellation_checker(self):
        """Пингует флаг отмены каждые 200мс."""
        while not self.worker.is_cancelled:
            await asyncio.sleep(0.2)
    
    def call_api(self, prompt, log_prefix, allow_incomplete=False, use_stream=True, debug=False, max_output_tokens=None):
        """
        Основной метод. Реализации должны вызывать self._get_or_create_session_internal().
        """
        raise NotImplementedError
    
    def _process_exception_and_counters(self, e: Exception):
        # 1. Специфичная логика для PartialGenerationError
        if isinstance(e, PartialGenerationError):
            # Проводим диагностику текста на зацикливание
            if self._detect_looping(e.partial_text):
                # ВМЕШАТЕЛЬСТВО: Если найден цикл, превращаем ошибку в ValidationFailedError.
                # Это заставит воркер сбросить текущий прогресс и начать генерацию заново,
                # вместо того чтобы продолжать дописывать повторяющийся бред.
                raise ValidationFailedError(f"Обнаружено зацикливание текста в прерванном ответе. Причина сброса: {e.reason}")
            
            # Если цикла нет - пробрасываем как есть (воркер попробует дописать)
            raise e
        
        
        # Стандартная обработка ошибок
        if isinstance(e, (
            OperationCancelledError, ContentFilterError, ValidationFailedError
        )):
            raise e

        self.worker.settings_manager.decrement_request_count(self.worker.api_key, self.worker.model_id)
        
        # ЛОГИКА СБРОСА СЕССИИ
        # Если это NetworkError или ошибка aiohttp, сбрасываем сессию,
        # так как коннектор может быть в "битом" состоянии.
        is_aiohttp_error = isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError, OSError))
        # Проверяем также по тексту ошибки, если она завернута
        error_text = str(e).lower()
        is_disconnect = "disconnected" in error_text or "connection" in error_text or "closed" in error_text
        
        if is_aiohttp_error or is_disconnect or isinstance(e, NetworkError):
            self._force_session_reset()
        
        if ("http error" in error_text and "403" in error_text) or ("http 403" in error_text):
            raise LocationBlockedError(f"Ошибка доступа 403") from e
        
        # Далее стандартная классификация для ретраев
        if isinstance(e, (
            NetworkError, TemporaryRateLimitError, RateLimitExceededError, LocationBlockedError, ModelNotFoundError
        )):
            raise e
        
        if "сannot connect to host" in error_text or "getaddrinfo failed" in error_text:
            raise NetworkError(f"Нет связи с сервером, или нет интернета.", delay_seconds=60) from e
        

        
        if isinstance(e, aiohttp.ClientResponseError) and e.status == 429:
            raise TemporaryRateLimitError(f"Превышен минутный лимит (код 429)", delay_seconds=65) from e
        
        if isinstance(e, aiohttp.ClientPayloadError):
            error_msg = "Сетевой сбой: Некорректный ответ Сервера."
            raise NetworkError(error_msg, delay_seconds=30) from e
        
        if isinstance(e, aiohttp.ClientResponseError) and e.status in [401, 403]:
            raise NetworkError(f"Доступ запрещен (код {e.status}): {e.reason}", delay_seconds=30) from e
        
        if 'api key not valid' in error_text or 'permission denied' in error_text:
            raise RateLimitExceededError(f"Невалидный/заблокированный API ключ {self.worker.api_key[-4:]}: {str(e)}") from e

        if "вам включили лимиты" in error_text or "quota_exceeded" in error_text:
            raise RateLimitExceededError(str(e))
        
        if "user location is not supported" in error_text:
            raise LocationBlockedError(f"Геоблок: {str(e)}") from e
            
        if "model" in error_text and ("not found" in error_text or "is not supported" in error_text):
            raise ModelNotFoundError(f"Модель не найдена: {str(e)}") from e
        
        if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError, RequestsError, OSError) + PROXY_ERRORS):
            error_msg = f"Сетевой сбой ({type(e).__name__}): {str(e)}"
            raise NetworkError(error_msg, delay_seconds=30) from e

        raise e
        
    def _detect_looping(self, text):
        """
        [Диагностика 2.0] Статистический анализ зацикливания.
        Проверяет периодичность повторов и кластеризацию повторяющихся блоков.
        """
        if not text: return False
        
        # 1. Парсинг и очистка
        raw_blocks = re.split(r'</p>|\n\s*\n', text)
        blocks = []
        for b in raw_blocks:
            clean = re.sub(r'<[^>]+>', '', b).strip()
            if clean: blocks.append(clean)
            
        if len(blocks) < 4: return False

        # 2. Картирование: Текст -> Список индексов вхождений
        index_map = {}
        for idx, block in enumerate(blocks):
            if block not in index_map: index_map[block] = []
            index_map[block].append(idx)

        # 3. Критерий А: Одиночный периодический цикл (Oscillation)
        # "Если один абзац имеет вхождения > 3 и дистанция повторяется в 70% случаев"
        for block, indices in index_map.items():
            count = len(indices)
            if count <= 1: continue
            
            # Для коротких фраз повышаем порог, чтобы не ловить "Он кивнул."
            is_short = len(block) < 30
            required_count = 5 if is_short else 4
            
            if count >= required_count:
                # Вычисляем дистанции (шаги) между вхождениями: [2, 5, 8] -> [3, 3]
                diffs = [indices[i+1] - indices[i] for i in range(len(indices)-1)]
                
                if not diffs: continue
                
                # Анализ частоты дистанций
                dist_counts = Counter(diffs)
                most_common_dist, freq = dist_counts.most_common(1)[0]
                
                # Если одна и та же дистанция встречается в >= 70% случаев -> это ритмичный цикл
                if freq / len(diffs) >= 0.70:
                    return True

        # 4. Критерий Б: Сценарная петля (Cluster Loop)
        # "Если три абзаца рядом имеют множественные вхождения"
        consecutive_repeats = 0
        
        for idx, block in enumerate(blocks):
            # Проверяем, встречается ли этот блок где-то еще в тексте
            if len(index_map[block]) > 1:
                consecutive_repeats += 1
            else:
                consecutive_repeats = 0
            
            # Если нашли цепочку из 3 подряд идущих блоков, которые есть где-то еще
            if consecutive_repeats >= 3:
                # Доп. проверка: суммарная длина должна быть значимой (исключаем диалог "Да/Нет/Да")
                b1 = blocks[idx]
                b2 = blocks[idx-1]
                b3 = blocks[idx-2]
                if (len(b1) + len(b2) + len(b3)) > 60:
                     return True

        return False
        
        
from abc import ABC, abstractmethod


class BaseServer(ABC):
    """
    Абстрактный базовый класс для локальных серверов (стратегия).
    Определяет интерфейс управления жизненным циклом и валидации.
    """
    def __init__(self, port=None):
        self.port = port
        self._is_running = False

    @abstractmethod
    def start(self, anonymous=True):
        """Запускает сервер в отдельном потоке."""
        pass

    @abstractmethod
    def stop(self):
        """Останавливает сервер."""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """Возвращает True, если сервер активен и отвечает."""
        pass

    @abstractmethod
    def get_url(self) -> str | None:
        """Возвращает базовый URL запущенного сервера (например http://127.0.0.1:PORT)."""
        pass
    
    @abstractmethod
    def validate_token(self, token: str) -> dict:
        """
        Проверяет один токен.
        Return: {'valid': bool, 'message': str, 'email': str, 'is_pro': bool}
        """
        pass

    @abstractmethod
    def validate_batch(self, tokens: list) -> list:
        """
        Проверяет список токенов.
        Return: list[dict] (результаты validate_token + поле 'token')
        """
        pass
