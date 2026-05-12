# gemini_translator/ui/widgets/status_bar_widget.py

from collections import Counter

from PyQt6 import QtCore, QtWidgets


STATUS_FLUSH_INTERVAL_MS = 100


class StatusBarWidget(QtWidgets.QWidget):
    """
    Виджет, отображающий многоцветный прогресс-бар и текстовый статус.
    """

    def __init__(self, parent=None, event_bus=None, engine=None):
        super().__init__(parent)
        self.success_count = 0
        self.in_progress_count = 0
        self.filtered_count = 0
        self.error_count = 0
        self.total_tasks = 0
        self._pending_task_state = None
        self._uses_topic_subscription = False

        self._init_ui()
        self._status_flush_timer = QtCore.QTimer(self)
        self._status_flush_timer.setSingleShot(True)
        self._status_flush_timer.timeout.connect(self._flush_pending_task_state)

        app = QtWidgets.QApplication.instance()

        self.bus = event_bus
        if not self.bus and hasattr(app, "event_bus"):
            self.bus = app.event_bus

        self.engine = engine
        if not self.engine and hasattr(app, "engine"):
            self.engine = app.engine

        if self.bus and hasattr(self.bus, "subscribe"):
            self.bus.subscribe("session_started", self._on_session_started)
            self.bus.subscribe("session_finished", self._on_session_finished)
            self.bus.subscribe("task_state_changed", self._on_task_state_changed)
            self._uses_topic_subscription = True
        elif self.bus and hasattr(self.bus, "event_posted"):
            self.bus.event_posted.connect(self.on_event)
        else:
            print("[StatusBarWidget WARN] Шина событий не предоставлена. Статус-бар не будет обновляться.")

    def _init_ui(self):
        main_layout = QtWidgets.QStackedLayout(self)
        main_layout.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        main_layout.setContentsMargins(0, 0, 0, 0)
        self.setFixedHeight(28)

        color_bar_widget = QtWidgets.QWidget()
        self.color_bar_layout = QtWidgets.QHBoxLayout(color_bar_widget)
        self.color_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.color_bar_layout.setSpacing(0)

        self.part_success = QtWidgets.QLabel()
        self.part_success.setStyleSheet("background-color: #2ECC71;")
        self.part_in_progress = QtWidgets.QLabel()
        self.part_in_progress.setStyleSheet("background-color: #3498DB;")
        self.part_filtered = QtWidgets.QLabel()
        self.part_filtered.setStyleSheet("background-color: #9B59B6;")
        self.part_error = QtWidgets.QLabel()
        self.part_error.setStyleSheet("background-color: #E74C3C;")
        self.part_pending = QtWidgets.QLabel()
        self.part_pending.setStyleSheet("background-color: #373e4b;")

        for part in [
            self.part_success,
            self.part_in_progress,
            self.part_filtered,
            self.part_error,
            self.part_pending,
        ]:
            self.color_bar_layout.addWidget(part, 0)

        self.progress_bar_text = QtWidgets.QProgressBar()
        self.progress_bar_text.setStyleSheet(
            """
            QProgressBar {
                background-color: transparent;
                border: 1px solid #4d5666;
                border-radius: 5px;
                text-align: center;
                color: #f0f0f0;
            }
            QProgressBar::chunk { background-color: transparent; }
        """
        )
        self.progress_bar_text.setTextVisible(True)

        self.temp_message_timer = QtCore.QTimer(self)
        self.temp_message_timer.setSingleShot(True)
        self.temp_message_timer.timeout.connect(self.clear_message)

        main_layout.addWidget(color_bar_widget)
        main_layout.addWidget(self.progress_bar_text)

        self.reset()
        self.setVisible(False)

    @QtCore.pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """Слушает глобальную шину и реагирует на нужные события."""
        event_name = event_data.get("event")

        if event_name == "session_started":
            self._on_session_started(event_data)

        elif event_name == "session_finished":
            self._on_session_finished(event_data)

        elif event_name == "task_state_changed":
            self._on_task_state_changed(event_data)

    def _on_session_started(self, event_data: dict):
        data = event_data.get("data", {})
        self.start_session(data.get("total_tasks", 0))

    def _on_session_finished(self, _event_data: dict):
        self.stop_session()

    def _on_task_state_changed(self, event_data: dict):
        data = event_data.get("data", {})
        full_state = data.get("full_state")
        self._pending_task_state = full_state if isinstance(full_state, list) else None
        if not self._status_flush_timer.isActive():
            self._status_flush_timer.start(STATUS_FLUSH_INTERVAL_MS)

    def _flush_pending_task_state(self):
        ui_state_list = self._pending_task_state
        self._pending_task_state = None

        if not isinstance(ui_state_list, list):
            if not (self.engine and self.engine.task_manager):
                return
            ui_state_list = self.engine.task_manager.get_ui_state_list()

        self._apply_task_state(ui_state_list)

    def _apply_task_state(self, ui_state_list):
        self.total_tasks = len(ui_state_list)
        self.progress_bar_text.setRange(0, self.total_tasks)

        status_counts = Counter(item[1] for item in ui_state_list)
        error_total = sum(count for status, count in status_counts.items() if "error" in status)
        in_progress_total = status_counts.get("in_progress", 0) + status_counts.get("completion", 0)

        self.update_counts(
            success=status_counts.get("success", 0) + status_counts.get("glossary_success", 0),
            in_progress=in_progress_total,
            filtered=status_counts.get("filtered", 0),
            error=error_total,
        )

    def start_session(self, total_tasks=0):
        """Вызывается в начале сессии для настройки."""
        self.reset()
        self.total_tasks = total_tasks
        self.progress_bar_text.setRange(0, total_tasks)
        self._update_display()
        self.setVisible(True)

    def stop_session(self):
        """Вызывается в конце сессии для сброса и скрытия."""
        self._pending_task_state = None
        self._status_flush_timer.stop()
        self.setVisible(False)

    def reset(self):
        """Сбрасывает все счетчики и виджеты в исходное состояние."""
        self.success_count = 0
        self.in_progress_count = 0
        self.filtered_count = 0
        self.error_count = 0
        self.total_tasks = 0
        self.progress_bar_text.setValue(0)
        self._update_display()

    def update_counts(self, success, in_progress, filtered, error):
        """Напрямую устанавливает количество задач каждого типа."""
        self.success_count = success
        self.in_progress_count = in_progress
        self.filtered_count = filtered
        self.error_count = error
        self._update_display()

    def _update_display(self):
        """Обновляет текстовое и графическое представление прогресс-бара."""
        if self.total_tasks == 0:
            self.progress_bar_text.setFormat("Нет задач")
            return

        processed_count = self.success_count + self.filtered_count + self.error_count
        self.progress_bar_text.setValue(processed_count)

        self.progress_bar_text.setFormat(
            f"Успех: {self.success_count} | В работе: {self.in_progress_count} | "
            f"Фильтр: {self.filtered_count} | Ошибки: {self.error_count} | "
            f"Готово: {processed_count}/{self.total_tasks} (%p%)"
        )

        self.part_success.setVisible(self.success_count > 0)
        self.part_in_progress.setVisible(self.in_progress_count > 0)
        self.part_filtered.setVisible(self.filtered_count > 0)
        self.part_error.setVisible(self.error_count > 0)

        remaining_count = max(0, self.total_tasks - processed_count - self.in_progress_count)
        self.part_pending.setVisible(remaining_count > 0)

        self.color_bar_layout.setStretch(0, self.success_count)
        self.color_bar_layout.setStretch(1, self.in_progress_count)
        self.color_bar_layout.setStretch(2, self.filtered_count)
        self.color_bar_layout.setStretch(3, self.error_count)
        self.color_bar_layout.setStretch(4, remaining_count)

    def show_message(self, message: str, temporary: bool = True, duration_ms: int = 3000):
        """
        Универсальный метод для отображения сообщений в статус-баре.

        :param message: Текст сообщения.
        :param temporary: Если True, сообщение исчезнет через duration_ms.
        :param duration_ms: Время отображения временного сообщения в миллисекундах.
        """
        if self.temp_message_timer.isActive():
            self.temp_message_timer.stop()

        self.progress_bar_text.setFormat(message)

        if temporary:
            self.temp_message_timer.start(duration_ms)

    def clear_message(self):
        """
        Очищает сообщение и возвращает отображение стандартного прогресса.
        """
        if self.temp_message_timer.isActive():
            self.temp_message_timer.stop()

        self._update_display()

    def set_permanent_message(self, message: str):
        """Отображает постоянное сообщение, которое не сбрасывается."""
        self.show_message(message, temporary=False)

    def closeEvent(self, event):
        """Отписываемся от шины при закрытии/уничтожении виджета."""
        if self.bus:
            try:
                if self._uses_topic_subscription and hasattr(self.bus, "unsubscribe"):
                    self.bus.unsubscribe("session_started", self._on_session_started)
                    self.bus.unsubscribe("session_finished", self._on_session_finished)
                    self.bus.unsubscribe("task_state_changed", self._on_task_state_changed)
                elif hasattr(self.bus, "event_posted"):
                    self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError, ValueError):
                pass
        self._status_flush_timer.stop()
        super().closeEvent(event)
