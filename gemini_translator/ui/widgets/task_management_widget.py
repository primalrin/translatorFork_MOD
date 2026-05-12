# gemini_translator/ui/widgets/task_management_widget.py
from collections import Counter
import traceback
from PyQt6 import QtWidgets, QtCore, QtGui # <-- ИЗМЕНЕНИЕ ЗДЕСЬ
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox
from PyQt6.QtCore import pyqtSignal, Qt, pyqtSlot

# Импортируем наш же виджет списка глав
from .chapter_list_widget import ChapterListWidget

class TaskManagementWidget(QWidget):
    """
    Виджет-вкладка для управления списком задач: отображение, пересборка, фильтрация.
    """
    # Сигналы, которые этот виджет будет "пробрасывать" наверх от дочерних виджетов
    tasks_changed = pyqtSignal()
    filter_all_translated_requested = pyqtSignal() 
    filter_validated_requested = pyqtSignal()
    filter_packaging_requested = pyqtSignal()
    validation_requested = pyqtSignal() 
    
    backup_restore_requested = pyqtSignal()
    
    # --- СИГНАЛЫ, ДЕЛЕГИРОВАННЫЕ ИЗ ChapterListWidget ---
    reorder_requested = pyqtSignal(str, list)
    duplicate_requested = pyqtSignal(list)
    remove_selected_requested = pyqtSignal(list)
    copy_originals_requested = pyqtSignal()
    reanimate_requested = pyqtSignal(list)
    split_batch_requested = pyqtSignal(list)
    batch_chapters_reorder_requested = pyqtSignal(object, list)
    chapter_preview_requested = pyqtSignal(str, str)
    
    # Карта фильтров ошибок: Название -> Ключ ошибки
    ERROR_FILTERS = {
        "Все задачи": None,
        "Только Блокировки": "CONTENT_FILTER",
        "Только Сеть": "NETWORK",
        "Только Валидация": "VALIDATION",
        "Частичная генерация": "PARTIAL_GENERATION",
        "Ошибки API": "API_ERROR",
        "Лимиты (Quota)": "QUOTA_EXCEEDED"
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_session_active = False # <--- НОВАЯ СТРОКА
        self._pending_ui_state = None
        self._uses_topic_subscription = False
        self.bus = None
        self._redraw_timer = QtCore.QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(35)
        self._redraw_timer.timeout.connect(self._do_redraw)
        self._init_ui()
        # --- ВСЕ СВЯЗИ ТЕПЕРЬ ВНУТРИ И ОНИ ПОЛНЫЕ ---
        self.chapter_list_widget.reorder_requested.connect(self.reorder_requested.emit)
        self.chapter_list_widget.duplicate_requested.connect(self.duplicate_requested.emit)
        self.chapter_list_widget.remove_selected_requested.connect(self.remove_selected_requested.emit)
        self.chapter_list_widget.copy_originals_requested.connect(self.copy_originals_requested.emit)
        self.chapter_list_widget.reanimate_requested.connect(self.reanimate_requested.emit)
        self.chapter_list_widget.split_batch_requested.connect(self.split_batch_requested.emit)
        self.chapter_list_widget.batch_chapters_reorder_requested.connect(self.batch_chapters_reorder_requested.emit)
        self.chapter_list_widget.chapter_preview_requested.connect(self.chapter_preview_requested.emit)
        # ---------------------------------------------
        app = QtWidgets.QApplication.instance()
        if hasattr(app, 'event_bus'):
            self.bus = app.event_bus
            if hasattr(self.bus, "subscribe"):
                self.bus.subscribe("task_state_changed", self._on_task_state_changed)
                self.bus.subscribe("session_finished", self._on_session_finished)
                self._uses_topic_subscription = True
            elif hasattr(self.bus, "event_posted"):
                self.bus.event_posted.connect(self.on_event)
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---
    
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # --- 1. Панель с кнопками действий ---
        action_panel_layout = QHBoxLayout()
        
        # Фильтр по категориям ошибок
        self.category_filter_combo = QComboBox()
        self.category_filter_combo.addItems(self.ERROR_FILTERS.keys())
        self.category_filter_combo.setToolTip("Фильтр задач по типу возникшей ошибки (история ошибок)")
        self.category_filter_combo.currentTextChanged.connect(self.redraw_ui)
        self.category_filter_combo.setMinimumWidth(150)
        action_panel_layout.addWidget(self.category_filter_combo)
        
        self.rebuild_tasks_btn = QPushButton("🔄 Пересобрать задачи")
        self.rebuild_tasks_btn.setToolTip(
            "Пересобрать пакеты или чанки, отменив все ручные изменения порядка и дублирования."
        )
        self.rebuild_tasks_btn.clicked.connect(self.tasks_changed.emit)
    
        self.filter_btn = QPushButton("Скрыть переведенные")
        self.filter_btn.setToolTip("Синхронизирует проект и скрывает задачи, для которых есть ЛЮБАЯ версия перевода.")
        self.filter_btn.clicked.connect(self.filter_all_translated_requested.emit)
    
        self.filter_validated_btn = QPushButton("Скрыть готовые")
        self.filter_validated_btn.setToolTip("Синхронизирует проект и скрывает задачи, для которых есть проверенная ('готовая') версия.")
        self.filter_validated_btn.clicked.connect(self.filter_validated_requested.emit)
        
        self.retry_filtered_btn = QPushButton("Обработка Фильтра")
        self.retry_filtered_btn.setToolTip(
            "Открыть диалог для обработки глав, заблокированных контент-фильтром.\n"
            "Позволяет создать специальные 'смешанные' пакеты или оставить только проблемные главы."
        )
        self.retry_filtered_btn.clicked.connect(self.filter_packaging_requested.emit)
        self.retry_filtered_btn.setVisible(False)
    
        self.validate_btn = QPushButton("✅ Проверить перевод")
        self.validate_btn.setToolTip("Открыть инструмент проверки и сравнения переводов")
        self.validate_btn.clicked.connect(self.validation_requested.emit)
        
        # --- ДОБАВИТЬ ЭТОТ БЛОК (Кнопка Бэкапа) ---
        self.backup_btn = QPushButton("💾 Очередь...")
        self.backup_btn.setToolTip("Сохранить текущую очередь задач на диск или загрузить сохраненную.")
        self.backup_btn.clicked.connect(self.backup_restore_requested.emit)
        # ------------------------------------------

        action_panel_layout.addWidget(self.rebuild_tasks_btn)
        action_panel_layout.addWidget(self.filter_btn)
        action_panel_layout.addWidget(self.filter_validated_btn)
        action_panel_layout.addWidget(self.validate_btn)
        
        # --- ДОБАВИТЬ В LAYOUT ---
        action_panel_layout.addWidget(self.backup_btn)
        # -------------------------
        
        action_panel_layout.addWidget(self.retry_filtered_btn)
        action_panel_layout.addStretch()
        
        
        
        
        
        
        main_layout.addLayout(action_panel_layout)
        
        # --- 2. Виджет со списком глав (без изменений) ---
        self.chapter_list_widget = ChapterListWidget(self)
        
        main_layout.addWidget(self.chapter_list_widget)
    
    def _log_ui_error(self, context, exc):
        error_text = f"[TASK UI ERROR] {context}: {type(exc).__name__}: {exc}"
        print(error_text)
        print(traceback.format_exc())
        app = QtWidgets.QApplication.instance()
        if hasattr(app, 'event_bus'):
            try:
                app.event_bus.event_posted.emit({
                    'event': 'log_message',
                    'source': 'TaskManagementWidget',
                    'data': {'message': error_text}
                })
            except Exception:
                pass

    def set_validation_enabled(self, enabled): # <-- НОВЫЙ МЕТОД
        """Управляет доступностью кнопки валидатора."""
        self.validate_btn.setEnabled(enabled)
    
    def set_retry_filtered_button_visible(self, visible):
        """Управляет видимостью кнопки 'Оставить только 'Фильтр'."""
        self.retry_filtered_btn.setVisible(visible)
        
        # --- ФИКС: Если кнопка появилась АСИНХРОННО после завершения сессии,
        # мы должны принудительно включить её, так как set_session_mode уже прошел.
        if visible and not self._is_session_active:
            self.retry_filtered_btn.setEnabled(True)
        
    def redraw_ui(self):
        """
        С НЕБОЛЬШОЙ ЗАДЕРЖКОЙ запрашивает актуальный список состояния
        у TaskManager и передает его в "умный" метод update_list.
        Задержка помогает избежать состояний гонки при чтении из БД.
        """
        self._redraw_timer.start()


    def set_session_mode(self, is_session_active):
        """
        Переключает доступность кнопок управления списком.
        """
        self._is_session_active = is_session_active # <--- ЗАПОМИНАЕМ СОСТОЯНИЕ
        
        # --- Кнопки, которые меняют структуру задач (блокируются) ---
        self.rebuild_tasks_btn.setEnabled(not is_session_active)
        self.filter_btn.setEnabled(not is_session_active)
        self.filter_validated_btn.setEnabled(not is_session_active)
        self.validate_btn.setEnabled(not is_session_active)
        self.validate_btn.setEnabled(not is_session_active)
        self.backup_btn.setEnabled(not is_session_active)
        self.category_filter_combo.setEnabled(not is_session_active)
        
        # --- Кнопка, зависящая от результатов сессии ---
        if is_session_active:
            self.retry_filtered_btn.setEnabled(False)
        else:
            # Видимость и доступность кнопки проверяется только по окончании сессии
            self.check_and_update_retry_button_visibility()
            # Если кнопка уже видна, разблокируем её
            if self.retry_filtered_btn.isVisible():
                self.retry_filtered_btn.setEnabled(True)
    
        # --- Делегируем управление состоянием дочернему виджету ---
        self.chapter_list_widget.set_session_mode(is_session_active)
        
        
    def on_event(self, event_data: dict):
        try:
            event_name = event_data.get('event')

            if event_name == 'task_state_changed':
                self._on_task_state_changed(event_data)

            if event_name == 'session_finished':
                self._on_session_finished(event_data)
        except Exception as e:
            self._log_ui_error("on_event", e)

    def _on_task_state_changed(self, event_data: dict):
        full_state = event_data.get('data', {}).get('full_state')
        if isinstance(full_state, list):
            self._pending_ui_state = full_state
        self.redraw_ui()

    def _on_session_finished(self, _event_data: dict):
        QtCore.QTimer.singleShot(100, self.check_and_update_retry_button_visibility)

    def _do_redraw(self):
        try:
            app = QtWidgets.QApplication.instance()
            if not (hasattr(app, 'engine') and app.engine and app.engine.task_manager):
                if self and self.chapter_list_widget:
                    self.chapter_list_widget.update_list([])
                return

            if self and self.chapter_list_widget:
                ui_state_list = self._pending_ui_state
                if not isinstance(ui_state_list, list):
                    ui_state_list = app.engine.task_manager.get_ui_state_list() or []
                self._pending_ui_state = None

                selected_filter = self.category_filter_combo.currentText()
                filter_key = self.ERROR_FILTERS.get(selected_filter)

                if filter_key:
                    filtered_list = []
                    for item in ui_state_list:
                        if not isinstance(item, tuple) or len(item) < 3:
                            continue
                        details = item[2] if isinstance(item[2], dict) else {}
                        errors_map = details.get('errors', {})
                        if filter_key in errors_map:
                            filtered_list.append(item)
                    self.chapter_list_widget.update_list(filtered_list)
                else:
                    self.chapter_list_widget.update_list(ui_state_list)

                self.check_and_update_retry_button_visibility()
        except Exception as e:
            self._log_ui_error("_do_redraw", e)

    def check_and_update_retry_button_visibility(self):
        found_filtered = False
        app = QtWidgets.QApplication.instance()

        try:
            if hasattr(app, 'engine') and app.engine and app.engine.task_manager:
                ui_state_list = app.engine.task_manager.get_ui_state_list() or []
                for _, status, details in ui_state_list:
                    if not isinstance(details, dict):
                        details = {}
                    if status == 'error' and 'CONTENT_FILTER' in details.get('errors', {}):
                        found_filtered = True
                        break
        except Exception as e:
            self._log_ui_error("check_and_update_retry_button_visibility", e)

        self.set_retry_filtered_button_visible(found_filtered)

    def update_list(self, tasks_data, original_filename=None):
        self.chapter_list_widget.update_list(tasks_data)
        self.check_and_update_retry_button_visibility()

    def closeEvent(self, event):
        """Отписываемся от шины при закрытии/уничтожении виджета."""
        if self.bus:
            try:
                if self._uses_topic_subscription and hasattr(self.bus, "unsubscribe"):
                    self.bus.unsubscribe("task_state_changed", self._on_task_state_changed)
                    self.bus.unsubscribe("session_finished", self._on_session_finished)
                elif hasattr(self.bus, "event_posted"):
                    self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError, ValueError):
                pass
        super().closeEvent(event)
