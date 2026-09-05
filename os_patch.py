# os_patch.py
# -*- coding: utf-8 -*-

"""
ARCHITECTURE NOTE: TRANSPARENT FILE SYSTEM PROXY (ROUTER PATTERN)
-----------------------------------------------------------------
Этот модуль реализует паттерн "Прозрачный Прокси" для системных вызовов ввода-вывода.
Он НЕ ломает стандартную библиотеку, а маршрутизирует вызовы на основе пути:

1. Пути с префиксом `mem://` -> Маршрутизируются в виртуальную FS в оперативной памяти (PyFilesystem2).
   Это обеспечивает Zero-Disk I/O для тяжелых операций (распаковка EPUB, чанкинг).

2. Обычные пути -> Маршрутизируются в нативные функции OS (через сохраненные оригиналы).

Это позволяет библиотекам (zipfile (отдельно модифицирован), lxml) работать с данными в RAM без изменения их кода,
просто принимая виртуальные пути.
"""

import os
import sys
import asyncio
import builtins
import threading
import traceback
import shutil
import uuid
import random
import warnings

# Suppress known third-party pkg_resources deprecation warnings from PyFilesystem2.
for _warning_category in (UserWarning, DeprecationWarning):
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API\..*",
        category=_warning_category,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"Deprecated call to `pkg_resources\.declare_namespace\('fs(?:\.[^']+)?'\)`\.",
        category=_warning_category,
    )

import fs
import zipfile
import sqlite3
import time
from collections import deque
from fs import path as fs_path
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtCore import QTimer

VIRTUAL_PREFIX = "mem://"

_original = {
    "open": builtins.open, "exists": os.path.exists, "listdir": os.listdir,
    "makedirs": os.makedirs, "remove": os.remove, "rename": os.rename,
    "replace": os.replace, "isdir": os.path.isdir, "isfile": os.path.isfile,
    "os_path": os.path,
    "sqlite3_connect": sqlite3.connect  # <--- ДОБАВИТЬ
}


# --- БЕЗОПАСНЫЙ GUI-НОТИФИКАТОР ---
class DeadlockNotifier(QtCore.QObject):
    """
    Мост для передачи сигнала о Deadlock из фонового потока в GUI.
    Должен быть инициализирован в главном потоке.
    """
    show_warning = QtCore.pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.show_warning.connect(self._on_show)
    
    def _on_show(self, title, text):
        # Этот слот выполнится в ГЛАВНОМ потоке
        try:
            # 1. Пытаемся разделить текст на "сообщение для людей" и "технический стек"
            # Разделитель, который мы использовали в acquire
            separator = "=== ИНФОРМАЦИЯ ДЛЯ ОТЛАДКИ"
            
            if separator in text:
                parts = text.split(separator, 1)
                human_text = parts[0].strip()
                # Возвращаем заголовок обратно
                technical_text = separator + parts[1]
            else:
                # Если разделителя нет, суем все в детали
                human_text = "Произошла блокировка ресурса. См. детали."
                technical_text = text

            # 2. Создаем и настраиваем окно
            msg = QtWidgets.QMessageBox()
            msg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            msg.setWindowTitle(title)
            msg.setText(human_text)
            
            # Это создает сворачиваемую область со скроллом!
            msg.setDetailedText(technical_text)
            
            # 3. Добавляем кнопки
            copy_btn = msg.addButton("Скопировать всё", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            close_btn = msg.addButton("Закрыть", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(close_btn)
            
            # 4. Логика копирования
            def copy_action():
                QtWidgets.QApplication.clipboard().setText(text)
                copy_btn.setText("Скопировано!")
                copy_btn.setEnabled(False)
            
            copy_btn.clicked.connect(copy_action)

            # 5. Делаем окно "поверх всех", чтобы пользователь точно заметил проблему
            msg.setWindowFlags(msg.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
            msg.exec()
            
        except Exception as e:
            # Фолбэк на консоль, если GUI сломался
            print(f"Не удалось показать окно DeadlockNotifier: {e}")
            print(f"Original message:\n{text}")

_global_notifier = None

# Non-isolated memfs copies are shared by every task that reads the same source
# file.  Keep the source identity next to the cached copy so replacing an EPUB
# at the same disk path cannot leave the task queue reading stale bytes.
_memfs_copy_lock = threading.RLock()
_memfs_source_signatures = {}

# --- КОД ВСПОМОГАТЕЛЬНЫХ ФУНКЦИЙ ---
def _parse_path(path):
    if isinstance(path, str) and path.startswith(VIRTUAL_PREFIX):
        mem_fs = _get_or_create_mem_fs()
        internal_path = path[len(VIRTUAL_PREFIX):]
        if not internal_path.startswith('/'):
            internal_path = '/' + internal_path
        return True, mem_fs, internal_path
    return False, None, path

def _safe_memfs_cleanup():
    app = QtWidgets.QApplication.instance()
    if hasattr(app, 'mem_fs') and app.mem_fs:
        print("--- [OS_PATCH] Закрытие виртуальной файловой системы... ---")
        app.mem_fs.close()

def _get_or_create_mem_fs():
    app = QtWidgets.QApplication.instance()
    if not hasattr(app, 'mem_fs'):
        print("--- [OS_PATCH] Открытие виртуальной файловой системы... ---")
        app.mem_fs = fs.open_fs('mem://')
        import atexit
        atexit.register(_safe_memfs_cleanup)
    return app.mem_fs

class HybridPath:
    def __getattr__(self, name):
        # --- НАЧАЛО ИЗМЕНЕНИЙ ---
        # 1. Проверяем, является ли запрашиваемый 'name' атрибутом, а не функцией,
        #    в оригинальном модуле os.path.
        if hasattr(_original["os_path"], name):
            original_attr = getattr(_original["os_path"], name)
            if not callable(original_attr):
                # Если это атрибут (как 'sep', 'altsep'), просто возвращаем его значение.
                # Важно: hasattr нужен, потому что getattr(..., None) вернет None, 
                # и мы не отличим отсутствие атрибута от атрибута со значением None (как altsep на Mac).
                return original_attr
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        # Если это не атрибут, то считаем, что это вызов функции, и возвращаем обертку.
        def wrapper(path, *args, **kwargs):
            is_virtual, _, internal_path = _parse_path(path)
            if is_virtual:
                mem_fs = _get_or_create_mem_fs()
                if name == 'exists':
                    return mem_fs.exists(internal_path)
                if name == 'isdir':
                    return mem_fs.isdir(internal_path)
                if name == 'isfile':
                    return mem_fs.isfile(internal_path)
                func = getattr(fs_path, name)
                result = func(internal_path, *args, **kwargs)
                if name in ('join', 'normpath', 'abspath') and isinstance(result, str):
                    if result.startswith('/'): result = result[1:]
                    return VIRTUAL_PREFIX + result
                return result
            else:
                func = getattr(_original["os_path"], name)

                # Для реальных путей полностью сохраняем нативное поведение Windows.
                # Это критично для device/UNC namespace путей вроде \\.\pipe\...,
                # которые использует asyncio при создании subprocess pipe.
                return func(path, *args, **kwargs)

        # Кэшируем обёртку в instance dict: повторные os.path.<name> идут в обход
        # __getattr__ (он вызывается только при промахе), маршрутизация mem://
        # остаётся внутри wrapper.
        wrapper.__name__ = name
        setattr(self, name, wrapper)
        return wrapper

# Отладочные стеки владельцев PatientLock (дорого: ~1 мс на захват).
_CAPTURE_OWNER_STACKS = os.environ.get("PATIENTLOCK_CAPTURE_STACKS") == "1"


class PatientLock:
    """
    Справедливый, СТРОГИЙ (НЕреентрантный) замок на базе Condition.
    Версия 30.0 ("Бронебойный"):
    1. Защита от IndexError в deque (пустая очередь).
    2. Защита от перезаписи владельца (_take_ownership возвращает статус).
    """
    _vip_threads = set()

    @classmethod
    def register_vip_thread(cls, thread_id):
        cls._vip_threads.add(thread_id)

    def __init__(self, timeout=30.0, global_timeout=20.0):
        self._mutex = threading.RLock()
        self._cond = threading.Condition(self._mutex)
        
        self._owner = None
        self._waiters = deque()
        
        self._timeout = timeout
        self._global_timeout = global_timeout
        
        self._owner_ts = None
        self._owner_stack = None
        
        self._current_leader = None
        self._leader_misses = 0

    def _take_ownership(self, thread_id):
        """
        Пытается присвоить владение.
        Возвращает True, если успешно.
        Возвращает False, если занято (и ставит поток в начало очереди).
        """
        # [DEFENSE] Защита от случайной перезаписи владельца
        if self._owner is not None:
            if self._owner != thread_id: # Если это не мы сами (рекурсия ловится выше)
                # Место занято! Отступаем в начало очереди (Приоритет)
                if thread_id not in self._waiters:
                    self._waiters.appendleft(thread_id)
                return False

        self._owner = thread_id
        self._owner_ts = time.monotonic()
        # Снятие полного стека стоит ~1 мс на КАЖДЫЙ захват; включается
        # только для отладки зависаний через переменную окружения.
        if _CAPTURE_OWNER_STACKS:
            self._owner_stack = traceback.format_stack()[:-2]
        else:
            self._owner_stack = None
        self._current_leader = None
        return True

    def acquire(self, priority=False):
        me = threading.get_ident()

        # ЛОКАЛЬНЫЕ переменные для слежки за лидером
        watched_leader = None
        leader_misses = 0 
        
        with self._mutex:
            # 1. ДИАГНОСТИКА РЕКУРСИИ
            if self._owner == me:
                current_stack = "".join(traceback.format_stack()[:-1])
                original_stack = "".join(self._owner_stack) if self._owner_stack else "<Стек потерян>"
                error_report = (
                    f"\n{'!'*80}\n[PatientLock] CRITICAL RECURSION DETECTED\n"
                    f"Поток {me} пытается захватить замок, которым УЖЕ владеет!\n"
                    f"1. Алиби:\n{original_stack}\n2. Преступление:\n{current_stack}\n{'!'*80}\n"
                )
                print(error_report)
                raise RuntimeError(error_report)

            # 2. БЫСТРЫЙ ПУТЬ
            if self._owner is None and not self._waiters:
                if self._take_ownership(me): return

            # 3. ВСТАЕМ В ОЧЕРЕДЬ
            if me not in self._waiters:
                if priority:
                    self._waiters.appendleft(me)
                else:
                    self._waiters.append(me)
            
            try:
                while True:
                    # --- [ZOMBIE RESURRECTION] ---
                    if self._owner != me and me not in self._waiters:
                        print(f" [PatientLock] Поток {me} воскрес и вернулся в очередь.")
                        self._waiters.append(me)

                    now = time.monotonic()

                    # --- СУД НАД ВЛАДЕЛЬЦЕМ ---
                    if self._owner is not None:
                        owner_limit = 60.0 if self._owner in self._vip_threads else 30.0
                        if self._owner_ts and (now - self._owner_ts) > owner_limit:
                            culprit_stack = "".join(self._owner_stack) if self._owner_stack else "<Стек не сохранен>"
                            print(f"\n{'!'*40}\n [PatientLock] СУД ЛИНЧА: Владелец {self._owner} сброшен ({owner_limit}с).\n{'!'*40}\n")
                            
                            global _global_notifier
                            if _global_notifier:
                                user_text = (
                                    f"ВНИМАНИЕ: Обнаружена блокировка ЗАМКА!\n\n"
                                    f"Поток (ID {self._owner}) удерживал ресурс более {owner_limit} с.\n"
                                    f"Приложение ПОПРОБУЕТ продолжить работу.\n\n"
                                    f"=== ИНФОРМАЦИЯ ДЛЯ ОТЛАДКИ ===\n{culprit_stack.strip()}"
                                )
                                _global_notifier.show_warning.emit("Deadlock Resolved (Watchdog)", user_text)
                            
                            self._owner = None
                            self._cond.notify_all()
                            continue
                    
                    # --- СУД НАД ЛИДЕРОМ ОЧЕРЕДИ ---
                    else: # Замок свободен (self._owner is None)
                        if not self._waiters:
                            if me not in self._waiters: self._waiters.append(me)
                            continue
                        
                        try: current_real_leader = self._waiters[0]
                        except IndexError: continue

                        # Если лидер сменился с прошлого раза -> сбрасываем слежку
                        if current_real_leader != watched_leader:
                            watched_leader = current_real_leader
                            leader_misses = 0 
                        
                        if current_real_leader == me:
                            if self._take_ownership(me): 
                                if me in self._waiters: self._waiters.remove(me)
                                return
                            else: 
                                leader_misses = 0
                                continue
                        else:
                            # Мы не лидер. Лидер пропустил ход.
                            leader_misses += 1
                            
                            miss_limit = 30 if current_real_leader in self._vip_threads else 10
                            
                            if leader_misses > miss_limit:
                                # Финальная проверка: лидер все еще тот же?
                                if self._waiters and self._waiters[0] == current_real_leader:
                                    print(f" [PatientLock] Поток {me} удалил спящего лидера {current_real_leader}.")
                                    self._waiters.remove(current_real_leader)
                                    watched_leader = None # Сбрасываем, чтобы на след круге взять нового
                                else:
                                    leader_misses = 0
                                
                                self._cond.notify_all()
                                continue

                    wait_timeout = 0.5 + random.uniform(0.0001, 0.005)
                    self._cond.wait(timeout=wait_timeout)
            
            except Exception as e:
                if me in self._waiters: self._waiters.remove(me)
                raise e

    def acquire_priority(self):
        self.acquire(priority=True)

    def release(self):
        me = threading.get_ident()
        with self._mutex:
            if self._owner != me:
                return
            
            self._owner = None
            self._owner_ts = None
            self._owner_stack = None
            self._current_leader = None
            
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
  
class PatientSQLiteConnection(sqlite3.Connection):
    
    def execute(self, *args, **kwargs):
        # Используем встроенный busy_timeout, но с циклом для подстраховки
        MAX_BUSY_WAIT_SECONDS = 15.0
        start_time = time.monotonic()
        
        while True:
            try:
                # Пытаемся выполнить операцию. 
                # busy_timeout, установленный при соединении, заставит SQLite
                # подождать, если база занята.
                return super().execute(*args, **kwargs)
            except sqlite3.OperationalError as e:
                error_text = str(e).lower()
                # Если, несмотря на busy_timeout, мы все равно получили ошибку
                # "locked" или "busy"...
                if "locked" in error_text or "busy" in error_text:
                    # ...проверяем, не вышли ли мы за общий лимит ожидания.
                    if time.monotonic() - start_time > MAX_BUSY_WAIT_SECONDS:
                        raise # Если вышли - пробрасываем ошибку
                    # Если не вышли - делаем короткую паузу и пытаемся снова.
                    # Это дает шанс другим потокам (особенно читателям) завершить работу.
                    time.sleep(0.05) 
                else:
                    # Если ошибка не связана с блокировкой - пробрасываем ее.
                    raise

    def executemany(self, *args, **kwargs):
        # Логика полностью аналогична execute
        MAX_BUSY_WAIT_SECONDS = 15.0
        start_time = time.monotonic()

        while True:
            try:
                return super().executemany(*args, **kwargs)
            except sqlite3.OperationalError as e:
                error_text = str(e).lower()
                if "locked" in error_text or "busy" in error_text:
                    if time.monotonic() - start_time > MAX_BUSY_WAIT_SECONDS:
                        raise
                    time.sleep(0.05)
                else:
                    raise

def _patched_open(file, mode='r', *args, **kwargs):
    is_virtual, mem_fs, internal_path = _parse_path(file)
    if is_virtual:
        try:
            if 'b' in mode: return mem_fs.openbin(internal_path, mode)
            encoding = kwargs.get('encoding', 'utf-8')
            return mem_fs.open(internal_path, mode, encoding=encoding)
        except fs.errors.ResourceNotFound:
             raise FileNotFoundError(f"No such file in memfs: '{file}'")

    # --- НАЧАЛО НОВОЙ ЛОГИКИ: "Терпеливое" открытие реальных файлов ---
    MAX_RETRIES = 5
    RETRY_DELAY_SECONDS = 0.25 # Начинаем с 0.25, потом растем
    last_exception = None

    for attempt in range(MAX_RETRIES):
        try:
            # Пытаемся открыть файл, используя оригинальную, непатченную функцию
            return _original["open"](file, mode, *args, **kwargs)
        except (IOError, PermissionError, OSError) as e:
            last_exception = e
            
            # Проверяем, не пытаемся ли мы открыть папку как файл (это фатально, ретраить бесполезно)
            if isinstance(e, PermissionError) and os.path.isdir(file):
                raise e

            # Анализ ошибки
            error_str = str(e).lower()
            
            # Список признаков временной блокировки
            # 13 = Permission Denied (часто бывает при блокировке антивирусом на запись)
            errno_val = getattr(e, 'errno', None)
            is_permission_denied = (errno_val == 13)
            
            is_locking_error = (
                "used by another process" in error_str 
                or "sharing violation" in error_str 
                or "lock" in error_str
                or is_permission_denied # <--- ВАЖНО: Добавляем общий PermissionDenied в список ретраев
            )
            
            # Если это последняя попытка, или ошибка не похожа на блокировку - сдаемся
            if attempt == MAX_RETRIES - 1:
                raise e
            
            if is_locking_error:
                wait_time = RETRY_DELAY_SECONDS * (attempt + 1)
                print(f"[OS_PATCH:open] Файл '{os.path.basename(str(file))}' недоступен (Errno: {errno_val}). Повтор {attempt + 1}/{MAX_RETRIES} через {wait_time}с...")
                time.sleep(wait_time)
                continue # Переходим к следующей попытке
            else:
                # Если ошибка какая-то экзотическая - пробрасываем сразу
                raise e
    
    # Этот код выполнится, только если цикл закончится без return (теоретически невозможно из-за raise)
    if last_exception:
        raise last_exception

def _patched_exists(path):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    if is_virtual:
        return mem_fs.exists(internal_path)
    return _original["exists"](path)

def _patched_listdir(path):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    return mem_fs.listdir(internal_path) if is_virtual else _original["listdir"](path)

def _patched_makedirs(path, *args, **kwargs):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    return mem_fs.makedirs(internal_path, *args, **kwargs) if is_virtual else _original["makedirs"](path, *args, **kwargs)

def _patched_remove(path):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    if is_virtual:
        return mem_fs.remove(internal_path)
    
    # --- ТЕРПЕЛИВОЕ УДАЛЕНИЕ ---
    MAX_RETRIES = 5
    for attempt in range(MAX_RETRIES):
        try:
            return _original["remove"](path)
        except (OSError, PermissionError) as e:
            if attempt == MAX_RETRIES - 1: raise e
            time.sleep(0.2 * (attempt + 1))

def _patched_isdir(path):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    return mem_fs.isdir(internal_path) if is_virtual else _original["isdir"](path)

def _patched_isfile(path):
    is_virtual, mem_fs, internal_path = _parse_path(path)
    return mem_fs.isfile(internal_path) if is_virtual else _original["isfile"](path)

def _patched_rename(src, dst):
    src_is_virtual, _, src_internal = _parse_path(src)
    dst_is_virtual, _, dst_internal = _parse_path(dst)
    
    if src_is_virtual and dst_is_virtual:
        if _patched_exists(dst):
            raise FileExistsError(f"Destination path '{dst}' already exists")
        return _get_or_create_mem_fs().move(src_internal, dst_internal)
    
    elif not src_is_virtual and not dst_is_virtual:
        # --- ТЕРПЕЛИВОЕ ПЕРЕИМЕНОВАНИЕ (NATIVE) ---
        MAX_RETRIES = 7 # Для переименования даем чуть больше попыток
        for attempt in range(MAX_RETRIES):
            try:
                return _original["rename"](src, dst)
            except (OSError, PermissionError) as e:
                # Если ошибка WinError 32 (занято) или 13 (доступ запрещен)
                error_str = str(e).lower()
                if "used by another process" in error_str or "sharing violation" in error_str or e.errno in (13, 32):
                    if attempt == MAX_RETRIES - 1: raise e
                    wait_time = 0.25 * (attempt + 1)
                    print(f"[OS_PATCH:rename] Файл занят, повтор {attempt+1}/{MAX_RETRIES} через {wait_time}с...")
                    time.sleep(wait_time)
                else:
                    raise e
    else:
        # Смешанный режим (Move между RAM и Disk)
        try:
            with _patched_open(src, 'rb') as f_src, _patched_open(dst, 'wb') as f_dst:
                shutil.copyfileobj(f_src, f_dst)
            _patched_remove(src)
        except Exception as e:
            raise OSError(f"Failed to move '{src}' to '{dst}': {e}") from e

def _patched_replace(src, dst):
    """
    Атомарная замена. В Windows os.replace часто кидает PermissionError, 
    если целевой файл существует и открыт кем-то на чтение.
    """
    # Если мы работаем с реальной ФС, пытаемся подготовить почву
    is_virtual_dst, _, _ = _parse_path(dst)
    if not is_virtual_dst and _patched_exists(dst):
        # Пытаемся удалить старый файл перед заменой, используя наш "терпеливый" remove
        try:
            _patched_remove(dst)
        except OSError:
            pass # Если не удалилось, rename ниже попробует сам или выкинет ошибку

    return _patched_rename(src, dst)

def _install_qt_message_handler():
    """
    Устанавливает перехватчик сообщений Qt для отладки ошибок многопоточности.
    Обновлено: Теперь ловит ошибки остановки таймеров (Stop) и обращения к детям (Parent).
    """
    from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
    import traceback

    def qt_message_handler(mode, context, message):
        # Формируем префикс как в стандартном выводе
        modes = {
            QtMsgType.QtDebugMsg: "Debug",
            QtMsgType.QtInfoMsg: "Info",
            QtMsgType.QtWarningMsg: "Warning",
            QtMsgType.QtCriticalMsg: "Critical",
            QtMsgType.QtFatalMsg: "Fatal"
        }
        mode_str = modes.get(mode, "Unknown")
        
        # Выводим само сообщение (чтобы не ломать стандартный лог)
        print(f"[Qt {mode_str}] {message}")

        # --- ЛОВУШКА ДЛЯ ТАЙМЕРОВ И ПОТОКОВ (РАСШИРЕННАЯ) ---
        msg_lower = message.lower()
        
        # Ловим классические ошибки Qt:
        # 1. QBasicTimer::start: Timers cannot be started from another thread
        # 2. QBasicTimer::stop: Failed. Possibly trying to stop from a different thread
        # 3. QObject::setParent: Cannot set parent, new parent is in a different thread
        # 4. QObject::killTimer: Timers cannot be stopped from another thread
        
        is_threading_error = (
            ("thread" in msg_lower) and 
            ("timer" in msg_lower or "parent" in msg_lower or "qobject" in msg_lower)
        )
        
        if is_threading_error:
            try:
                if "_patched_qmessagebox_critical" in traceback.print_stack():
                    print("\n" + "!"*80)
                    return
            except:
                pass
            print("\n" + "!"*80)
            print("[SHERLOCK] ПОЙМАНА ОПАСНАЯ ОПЕРАЦИЯ С QT ИЗ ЧУЖОГО ПОТОКА!")
            print(f"   Тип события: {mode_str}")
            print(f"   Сообщение движка: {message}")
            print("   ВИНОВНИК (Python Traceback в момент вызова метода Qt):")
            print("-" * 80)
            # Выводим стек вызовов Python. Это покажет строку кода в вашем скрипте,
            # которая дернула метод Qt, вызвавший ошибку.
            try:
                traceback.print_stack()
            except:
                print("traceback.print_stack() не обнаружен") 
            print("!"*80 + "\n")

    # Устанавливаем наш обработчик
    qInstallMessageHandler(qt_message_handler)

def _patched_zipfile_init(self, *args, **kwargs):
    """
    Умный патч для zipfile.ZipFile.__init__.
    Если на вход подается путь (строка), он самостоятельно вызывает
    пропатченный _patched_open для получения файлового объекта.
    """
    if args and isinstance(args[0], str):
        file_path = args[0]
        mode = args[1] if len(args) > 1 else 'r'
        binary_mode = mode.replace('b', '') + 'b'
        
        # Явно вызываем наш перехватчик, а не глобальный open()
        file_obj = _patched_open(file_path, binary_mode)
        
        new_args = (file_obj,) + args[1:]
        return _original['zipfile_init'](self, *new_args, **kwargs)

    return _original['zipfile_init'](self, *args, **kwargs)
    
    
def _patched_sqlite3_connect(*args, **kwargs):
    """
    Обертка для sqlite3.connect, которая подменяет создаваемый класс
    на наш 'PatientSQLiteConnection' для in-memory баз.
    """
    db_name_or_uri = args[0] if args else kwargs.get("database", "")
    is_our_shared_db = "mode=memory" in db_name_or_uri and "cache=shared" in db_name_or_uri

    if is_our_shared_db:
        kwargs['factory'] = PatientSQLiteConnection
    
    conn = _original["sqlite3_connect"](*args, **kwargs)

    # Применяем PRAGMA к нашему PatientSQLiteConnection
    if is_our_shared_db:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            # Устанавливаем таймаут ожидания в 15 секунд. 
            # SQLite будет сам пытаться получить доступ в течение этого времени.
            conn.execute("PRAGMA busy_timeout = 15000;")
        except sqlite3.OperationalError:
            pass # Ошибки здесь не критичны

    return conn

def _real_file_signature(real_path: str):
    stat_result = os.stat(real_path)
    return (
        stat_result.st_size,
        getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000)),
        getattr(stat_result, "st_ctime_ns", int(stat_result.st_ctime * 1_000_000_000)),
    )


def copy_to_mem(real_path: str, *, unique: bool = False, refresh: bool = False) -> str | None:
    # Используем _patched_exists, который теперь тоже защищен, так как использует _patched_open опосредованно
    if not real_path or not _patched_exists(real_path):
        return None
    
    mem_fs = _get_or_create_mem_fs()
    # Нормализация пути остается важной
    normalized_path = _original["os_path"].abspath(real_path).replace(":", "_drive").replace("\\", "/")
    if normalized_path.startswith('/'):
        normalized_path = normalized_path[1:]
    if unique:
        normalized_path = f"isolated/{uuid.uuid4().hex}/{normalized_path}"
    virtual_path_internal_for_pyfs = "/" + normalized_path
    virtual_path_for_return = VIRTUAL_PREFIX + normalized_path
    
    source_is_virtual = isinstance(real_path, str) and real_path.startswith(VIRTUAL_PREFIX)
    signature_key = (id(mem_fs), virtual_path_internal_for_pyfs)

    with _memfs_copy_lock:
        destination_exists = mem_fs.exists(virtual_path_internal_for_pyfs)
        source_signature = None
        if not source_is_virtual:
            try:
                source_signature = _real_file_signature(real_path)
            except OSError as e:
                print(f"[OS_PATCH ERROR] Не удалось прочитать метаданные {real_path}: {e}")
                return None

        needs_copy = bool(refresh or not destination_exists)
        if not unique and source_signature is not None:
            if _memfs_source_signatures.get(signature_key) != source_signature:
                needs_copy = True
            elif destination_exists:
                try:
                    cached_size = mem_fs.getinfo(
                        virtual_path_internal_for_pyfs,
                        namespaces=["details"],
                    ).size
                    if cached_size != source_signature[0]:
                        needs_copy = True
                except Exception:
                    needs_copy = True

        if not needs_copy:
            return virtual_path_for_return

        try:
            mem_fs.makedirs(_original["os_path"].dirname(virtual_path_internal_for_pyfs), recreate=True)
            # Теперь этот вызов _patched_open автоматически будет "терпеливым"
            with _patched_open(real_path, 'rb') as f_real:
                source_bytes = f_real.read()
            mem_fs.writebytes(virtual_path_internal_for_pyfs, source_bytes)
            if not unique and source_signature is not None:
                # Re-read the signature after the copy.  If an external writer
                # changed the source during the read, the next call will detect
                # the mismatch and refresh again instead of blessing stale data.
                final_signature = _real_file_signature(real_path)
                if final_signature == source_signature:
                    _memfs_source_signatures[signature_key] = final_signature
                else:
                    _memfs_source_signatures.pop(signature_key, None)
        except Exception as e:
            print(f"[OS_PATCH ERROR] Не удалось скопировать {real_path} в memfs: {e}")
            return None

    return virtual_path_for_return

def write_bytes_to_mem(data: bytes, extension: str = ".bin") -> str | None:
    mem_fs = _get_or_create_mem_fs()
    unique_name = f"/{uuid.uuid4().hex}{extension}"
    try:
        mem_fs.writebytes(unique_name, data)
        return VIRTUAL_PREFIX + unique_name
    except Exception as e:
        print(f"[OS_PATCH ERROR] Не удалось записать байты в memfs: {e}")
        return None
        
def copy_from_mem(virtual_path: str, real_path_dest: str) -> bool:
    if not isinstance(virtual_path, str) or not virtual_path.startswith(VIRTUAL_PREFIX): return False
    mem_fs = _get_or_create_mem_fs()
    virtual_path_internal = virtual_path[len(VIRTUAL_PREFIX):]
    if not mem_fs.exists(virtual_path_internal): return False
    try:
        real_dest_dir = _original["os_path"].dirname(real_path_dest)
        if real_dest_dir: _original["makedirs"](real_dest_dir, exist_ok=True)
        with mem_fs.openbin(virtual_path_internal) as f_src:
            with _original["open"](real_path_dest, 'wb') as f_dst:
                shutil.copyfileobj(f_src, f_dst)
        return True
    except Exception as e:
        print(f"[OS_PATCH ERROR] Не удалось скопировать {virtual_path} в {real_path_dest}: {e}")
        return False

_console_io_lock = threading.Lock()

def _force_console_and_print(title, text):
    """
    АВАРИЙНЫЙ МЕТОД: Открывает консоль и блокирует поток до вмешательства пользователя.
    Использует Lock, чтобы предотвратить скроллинг текста при лавине ошибок.
    """
    import sys
    import ctypes
    
    # Пытаемся захватить управление консолью.
    # Если консоль уже занята другой ошибкой, этот поток "уснет" здесь и будет ждать своей очереди.
    with _console_io_lock:
        
        # 1. Дублируем в stderr (для IDE)
        try:
            print(f"\n[CRITICAL FALLBACK] {title}\n{text}", file=sys.stderr)
        except:
            pass

        if sys.platform == "win32":
            try:
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                
                # Создаем консоль только если её нет
                if kernel32.GetConsoleWindow() == 0:
                    kernel32.AllocConsole()
                    sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                    sys.stderr = open("CONOUT$", "w", encoding="utf-8")
                    sys.stdin = open("CONIN$", "r", encoding="utf-8")
                
                # Звуковой сигнал
                ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
                
                print("\n" + "!"*80)
                print(f"[CRITICAL ERROR] (THREAD LOCKED): {title}")
                print("-" * 80)
                print(text)
                print("!"*80)
                
                print("\n" + "="*40)
                print(">>> АВТОМАТИЧЕСКАЯ ПАУЗА <<<")
                print("Вывод заблокирован. Другие потоки ожидают очереди.")
                print("Вы можете спокойно прочитать текст выше.")
                print("Нажмите [ENTER], чтобы пропустить эту ошибку и показать следующую (если есть).")
                print("Или закройте окно, чтобы убить программу.")
                print("="*40 + "\n")
                
                try:
                    # Это и есть наша "автоматическая пауза".
                    # Пока вы не нажмете Enter, Lock не освободится, 
                    # и другие потоки не смогут написать ни строчки.
                    input("Нажмите Enter для продолжения... ")
                except Exception:
                    import time
                    while True: time.sleep(1)
                
            except Exception as e:
                # Если создание консоли упало, пишем файл
                try:
                    import os
                    desktop = os.path.join(os.path.join(os.environ['USERPROFILE']), 'Desktop')
                    with open(os.path.join(desktop, "CRITICAL_ERROR_LOG.txt"), "a", encoding="utf-8") as f:
                        f.write(f"\n\n{title}\n{text}\nConsole failed: {e}")
                except:
                    pass

def _patched_qmessagebox_critical(parent, title, text):
    """
    Критическое окно с защитой от 'Error Storm' (шторм ошибок) и зависания GUI.
    """
    # Инициализируем статический счетчик, если его нет
    if not hasattr(_patched_qmessagebox_critical, "active_count"):
        _patched_qmessagebox_critical.active_count = 0

    # --- ЗАЩИТА ОТ КАСКАДА ОШИБОК ---
    # Если одно окно уже открыто, не пытаемся открыть второе (оно может перекрыть первое или зависнуть).
    # Сразу кидаем в консоль.
    if _patched_qmessagebox_critical.active_count > 0:
        _force_console_and_print(f"{title} [CASCADE/RECURSIVE ERROR]", text)
        # Возвращаем код отмены, так как GUI не был показан
        return QtWidgets.QMessageBox.StandardButton.Abort

    # Увеличиваем счетчик активных окон
    _patched_qmessagebox_critical.active_count += 1
    
    try:
        # 1. Подготовка сообщения
        parts = text.split('\n\n', 1)
        header = parts[0]
        details = text if len(parts) < 2 else text

        # Ошибки бывают на десятки/сотни строк: видимая часть окна
        # ограничивается, полный текст остаётся в раскрывающейся
        # прокручиваемой области «Show Details…» (и в «Скопировать ошибку»).
        header_lines = header.splitlines()
        if len(header_lines) > 8 or len(header) > 700:
            header = "\n".join(header_lines[:8]).rstrip()
            if len(header) > 700:
                header = header[:700].rstrip()
            header += "\n… (полный текст — в «Показать подробности» или по кнопке копирования)"

        main_text = (
            f"{header}\n\n"
            "Система обнаружила критическую ошибку.\n"
            "ЗАЩИТА: Если это окно зависнет или возникнут новые ошибки, откроется консоль."
        )

        msg_box = QtWidgets.QMessageBox(parent)
        msg_box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        msg_box.setWindowTitle(title)
        msg_box.setText(main_text)
        msg_box.setDetailedText(details)
        
        # 2. Кнопки
        copy_btn = msg_box.addButton("Скопировать ошибку", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        close_btn = msg_box.addButton("Закрыть", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        kill_btn = msg_box.addButton("Kill Process", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        msg_box.setDefaultButton(close_btn)
        
        # 3. Логика копирования
        def copy_action():
            QtWidgets.QApplication.clipboard().setText(text)
            copy_btn.setText("Скопировано!")
            copy_btn.setEnabled(False)
            reset_timer = getattr(msg_box, "_copy_reset_timer", None)
            if reset_timer is None:
                reset_timer = QTimer(msg_box)
                reset_timer.setSingleShot(True)

                def reset_copy_button():
                    copy_btn.setText("Скопировать ошибку")
                    copy_btn.setEnabled(True)

                reset_timer.timeout.connect(reset_copy_button)
                msg_box._copy_reset_timer = reset_timer

            reset_timer.start(2000)
            return
        
        copy_btn.clicked.connect(copy_action)
        kill_btn.clicked.connect(lambda: os._exit(1))
        
        # 4. WATCHDOG (Сторожевой пес) - защита от зависания самого GUI
        shared_state = {"last_beat": time.monotonic(), "running": True}
        
        # GUI Heartbeat
        heartbeat_timer = QTimer(msg_box)
        heartbeat_timer.timeout.connect(lambda: shared_state.update({"last_beat": time.monotonic()}))
        heartbeat_timer.start(500)
        
        def watchdog_guard():
            time.sleep(1.5)
            while shared_state["running"]:
                time.sleep(1.0)
                if not shared_state["running"]: break
                
                # Если пульс GUI пропал на 5 секунд
                if time.monotonic() - shared_state["last_beat"] > 5.0:
                    _force_console_and_print(f"{title} [GUI DEADLOCK]", text)
                    shared_state["running"] = False
                    break
                    
        t_dog = threading.Thread(target=watchdog_guard, daemon=True)
        t_dog.start()
        
        return msg_box.exec()
        
    except Exception as e:
        # Если само создание окна упало, тоже пишем в консоль
        _force_console_and_print(f"{title} [FAILED TO SHOW GUI]", f"Error showing box: {e}\nOriginal: {text}")
        return QtWidgets.QMessageBox.StandardButton.Abort
        
    finally:
        # Всегда освобождаем счетчик при выходе, даже при ошибке
        _patched_qmessagebox_critical.active_count -= 1
        
        # Очистка ресурсов watchdog
        if 'shared_state' in locals():
            shared_state["running"] = False
        if 'heartbeat_timer' in locals():
            heartbeat_timer.stop()

def get_original(name: str):
    """
    Публичный, безопасный интерфейс для доступа к оригинальным,
    непатченным функциям, сохраненным в словаре _original.
    """
    return _original.get(name)




def apply():
    if hasattr(builtins, '_os_patched'): return
    print("--- [Архитектурный Патч] Применение универсального патча для файловой системы... ---")
    
    # --- ИНИЦИАЛИЗАЦИЯ НОТИФИКАТОРА (НОВОЕ) ---
    global _global_notifier
    # Проверяем наличие QApplication, так как apply может вызываться в тестах без GUI
    if QtWidgets.QApplication.instance() and _global_notifier is None:
        _global_notifier = DeadlockNotifier()
    # -------------------------------------------
    
    os.path = HybridPath()
    builtins.open = _patched_open
    os.exists = _patched_exists
    os.listdir = _patched_listdir
    os.makedirs = _patched_makedirs
    os.remove = _patched_remove
    os.rename = _patched_rename
    os.replace = _patched_replace
    os.isdir = _patched_isdir
    os.isfile = _patched_isfile
    os.copy_to_mem = copy_to_mem
    os.write_bytes_to_mem = write_bytes_to_mem
    os.copy_from_mem = copy_from_mem
    
    print("--- [Архитектурный Патч] Имплантация адаптера в zipfile.ZipFile... ---")
    _original['zipfile_init'] = zipfile.ZipFile.__init__
    zipfile.ZipFile.__init__ = _patched_zipfile_init
    _install_qt_message_handler()
    
    if sys.platform == "win32":
        try:
            print("--- [Архитектурный Патч] Применение патча для asyncio.WindowsSelectorEventLoopPolicy... ---")
            loop_class = asyncio.WindowsSelectorEventLoopPolicy._loop_factory
            _original['windows_selector_loop_close'] = loop_class.close

            def _patched_windows_loop_close(self):
                if not hasattr(self, '_ssock') or self._ssock is None:
                    return
                _original['windows_selector_loop_close'](self)

            loop_class.close = _patched_windows_loop_close
            
        except (ImportError, AttributeError) as e:
            print(f"--- [Архитектурный Патч] Не удалось применить патч для asyncio: {e} ---")
    
    print("--- [Архитектурный Патч] Применение патча для QMessageBox.critical... ---")
    _original["qmessagebox_critical"] = QtWidgets.QMessageBox.critical
    QtWidgets.QMessageBox.critical = _patched_qmessagebox_critical
    
    
    
    print("--- [Архитектурный Патч] Применение патча для sqlite3.connect... ---")
    sqlite3.connect = _patched_sqlite3_connect
    
    builtins._os_patched = True
    print("--- [Архитектурный Патч] 'os' теперь полностью поддерживает 'mem://'. ---")
