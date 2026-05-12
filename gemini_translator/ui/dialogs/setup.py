# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# Диалоги начальной настройки
# ---------------------------------------------------------------------------
# Этот файл содержит единый класс диалогового окна для первоначальной
# настройки и запуска различных режимов работы приложения.
# ---------------------------------------------------------------------------

import os
import re
import json
import sqlite3
import uuid
import zipfile
from bs4 import BeautifulSoup
from collections import Counter
import math  # <--- ДОБАВЬТЕ ЭТУ СТРОКУ
import traceback # <--- ДОБАВЬТЕ ЭТУ СТРОКУ

# --- Импорты из PyQt6 ---
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QListWidget, QPushButton, QDialogButtonBox, QLabel,
    QTextEdit, QFileDialog, QDoubleSpinBox, QListWidgetItem, QCheckBox,
    QMessageBox, QStyle, QColorDialog,
    QTableWidget, QTableWidgetItem, QGroupBox, QFormLayout, QHBoxLayout, QHeaderView,
    QScrollArea, QWidget, QTabWidget, QGridLayout,
    QPlainTextEdit, QComboBox, QSpinBox, QSplitter, QAbstractItemView, QFrame
)

from PyQt6.QtCore import QMimeData, pyqtSlot, pyqtSignal, QThread, QItemSelectionModel, QItemSelection
from ...scripts.package_filter_tasks import FilterPackagingDialog

# --- Импорты из нашего проекта ---
from ...api import config as api_config
from ...api.managers import ApiKeyManager
from ...core.translation_engine import TranslationEngine
from ...core.task_manager import ChapterQueueManager, TaskDBWorker
from ...utils.settings import SettingsManager
from ...utils.epub_tools import (
    extract_number_from_path,
    calculate_potential_output_size,
    get_epub_chapter_sizes_with_cache,
    get_epub_chapter_order,
)
from ...utils.helpers import TokenCounter
from ...utils.language_tools import SmartGlossaryFilter, GlossaryReplacer
from ...utils.project_migrator import ProjectMigrator
from ...utils.project_manager import TranslationProjectManager
from ...utils.translated_paths import build_translated_output_path

from ..themes import (
    THEME_SETTINGS_KEY,
    build_dark_stylesheet,
    editable_theme_colors,
    extract_theme_colors,
)
from ..widgets import (
    KeyManagementWidget, TranslationOptionsWidget, ModelSettingsWidget,
    ProjectPathsWidget, GlossaryWidget, PresetWidget, ProjectActionsWidget,
    TaskManagementWidget, LogWidget, StatusBarWidget, ManualTranslationWidget,
    AutoTranslateWidget
)
from ..widgets.common_widgets import NoScrollSpinBox
from .epub import EpubHtmlSelectorDialog, TranslatedChaptersManagerDialog
from .misc import ProjectHistoryDialog, ProjectFolderDialog, GeoBlockDialog
from .menu_utils import post_session_separator, prompt_return_to_menu, return_to_main_menu
from .glossary import MainWindow as GlossaryToolWindow
from .glossary import ImporterWizardDialog
from .auto_workflow import (
    AutoConsistencyWorker,
    choose_preferred_translation_rel_path,
    load_project_chapters_for_consistency,
)
from datetime import datetime
import time # <-- НОВЫЙ ИМПОРТ


# --- НОВЫЕ КОНСТАНТЫ ДЛЯ КАЛИБРОВКИ ---
BENCHMARK_GLOSSARY_SIZE = 100    # Увеличиваем количество терминов
BENCHMARK_TEXT_SIZE = 10000     # Увеличиваем размер текста
BASE_GLOSSARY_PROMPT_STATE_FILE = "base_glossary_prompt_state.json"
AUTO_CJK_SHORT_RATIO_LIMIT = 1.80
AUTO_CJK_CHAR_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
# --- КОНЕЦ НОВЫХ КОНСТАНТ ---

def _format_duration(seconds: float) -> str:
    """Formats a rough duration estimate for display."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} сек"
    return f"{secs} сек"


class PreflightEstimateDialog(QDialog):
    """Compact dialog for previewing the session estimate before launch."""

    def __init__(self, report_text: str, can_start: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Предварительная оценка сессии")
        self.setMinimumSize(760, 540)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Ниже показана приблизительная оценка по текущим настройкам проекта. "
            "Значения по времени и стоимости являются ориентировочными."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        report_edit = QPlainTextEdit(self)
        report_edit.setReadOnly(True)
        report_edit.setPlainText(report_text)
        report_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(report_edit, 1)

        buttons = QDialogButtonBox(self)
        if can_start:
            start_button = buttons.addButton("Запустить", QDialogButtonBox.ButtonRole.AcceptRole)
            start_button.setDefault(True)
        close_label = "Отмена" if can_start else "Закрыть"
        buttons.addButton(close_label, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class BaseGlossarySelectionDialog(QDialog):
    """Lets the user choose one or more built-in glossaries for an empty project."""

    def __init__(self, glossary_options: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Базовый глоссарий")
        self.setMinimumWidth(520)
        self._skipped = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Глоссарий проекта пуст. Можно сразу добавить один или несколько базовых наборов терминов."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list_widget = QListWidget()
        for option in glossary_options:
            item = QListWidgetItem(f"{option['name']} ({option['count']} записей)")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, option['id'])
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        hint = QLabel("Выбор будет сохранён для этого проекта, чтобы окно не появлялось повторно.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)

        self.button_box = QDialogButtonBox()
        self.add_button = self.button_box.addButton("Добавить выбранные", QDialogButtonBox.ButtonRole.AcceptRole)
        self.skip_button = self.button_box.addButton("Пропустить", QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_button = self.button_box.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        self.button_box.clicked.connect(self._on_button_clicked)
        layout.addWidget(self.button_box)

    def selected_ids(self) -> list:
        selected = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                selected.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return selected

    def skipped(self) -> bool:
        return self._skipped

    def _on_button_clicked(self, button):
        if button == self.add_button:
            if not self.selected_ids():
                QMessageBox.information(self, "Ничего не выбрано", "Выберите хотя бы один глоссарий или нажмите «Пропустить».")
                return
            self.accept()
        elif button == self.skip_button:
            self._skipped = True
            self.accept()
        else:
            self.reject()


class ChapterTextPreviewDialog(QDialog):
    """Простой просмотрщик главы с пометкой источника."""

    def __init__(
        self,
        title: str,
        chapter_path: str,
        text_content: str,
        parent=None,
        render_html: bool = False,
        path_caption: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title or "Предпросмотр главы")
        self.setMinimumSize(760, 560)

        layout = QVBoxLayout(self)

        path_label = QLabel(path_caption or chapter_path)
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        text_edit = QPlainTextEdit(self)
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        text_edit.setFont(QtGui.QFont("Consolas", 10))
        text_edit.setPlainText(text_content)
        layout.addWidget(text_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class InitialSetupDialog(QDialog):
    """
    Единый диалог для настройки перевода.
    """
    tasks_changed = pyqtSignal()
    def __init__(self, parent=None, prefill_data=None):
        super().__init__(parent)

        # --- Флаги и базовые атрибуты (быстрая инициализация) ---
        self._initial_show_done = False
        self.prefill_data = prefill_data


        self.setMinimumSize(700, 550) # Компактный размер
        self.setWindowFlags(
            QtCore.Qt.WindowType.Dialog |
            QtCore.Qt.WindowType.WindowMinimizeButtonHint |
            QtCore.Qt.WindowType.WindowMaximizeButtonHint |
            QtCore.Qt.WindowType.WindowCloseButtonHint
        )
        self._apply_initial_geometry()

        app = QtWidgets.QApplication.instance()
        self.app = app

        self.version = ""
        if app and app.global_version:
            self.version = app.global_version
        self.setWindowTitle(f"Настройка сессии перевода {self.version}")

        self.settings_manager = app.get_settings_manager()
        self.context_manager = app.context_manager
        self.bus = app.event_bus
        self.engine = app.engine
        self.engine_thread = app.engine_thread
        self.task_manager = app.task_manager if hasattr(app, 'task_manager') else None
        self._translator_only_mode = os.environ.get("GT_TRANSLATOR_ONLY_MODE", "").strip() == "1"
        self.proxy_status_label = None
        self.proxy_button = None
        self.theme_color_buttons = {}
        self._ui_theme_colors = editable_theme_colors(
            extract_theme_colors(self.settings_manager.load_full_session_settings())
            or extract_theme_colors(self.settings_manager.load_settings())
        )

        self.selected_file = None
        self.html_files = []
        self.output_folder = None
        self.project_manager = None
        self.is_session_active = False
        self.current_project_folder_loaded = None # <--- ДОБАВЬТЕ ЭТУ СТРОКУ
        self.is_settings_dirty = False
        self.is_glossary_dirty = False
        self.local_set = False
        self.cpu_performance_index = None
        self.is_fuzzy_disabled_by_system = False
        self.global_settings = None

        self.initial_glossary_state = []
        self.active_session_id = None
        self.this_dialog_started_the_session = False # <<< ДОБАВЬТЕ ЭТУ СТРОКУ
        self.is_blocked_by_child_dialog = False # <<< ДОБАВЬТЕ ЭТУ СТРОКУ
        self._hard_stop_enabled = False
        self._snapshot_autosave_worker = None
        self._snapshot_restore_in_progress = False
        self._snapshot_prompted_projects = set()
        self._snapshot_save_requested = False
        self._base_glossary_prompt_seen_projects = set()
        self._pending_old_project_cleanup_offer = False
        self._returning_to_main_menu = False
        self._auto_workflow_enabled_for_session = False
        self._auto_workflow_round = 0
        self._auto_followup_running = False
        self._auto_last_retry_signatures = set()
        self._auto_last_untranslated_fix_signatures = set()
        self._auto_pending_network_retry_chapters = set()
        self._auto_filter_repack_signatures = set()
        self._auto_filter_redirect_signatures = set()
        self._auto_restart_session_override = None
        self._auto_validator_dialog = None
        self._auto_consistency_worker = None
        self._auto_glossary_dialog = None
        self._auto_glossary_running = False
        self._auto_glossary_pending_translation = False
        self._auto_glossary_completed = False

        self._auto_glossary_poll_timer = QtCore.QTimer(self)
        self._auto_glossary_poll_timer.setInterval(400)
        self._auto_glossary_poll_timer.timeout.connect(self._poll_auto_glossary_dialog)

        self._snapshot_save_timer = QtCore.QTimer(self)
        self._snapshot_save_timer.setSingleShot(True)
        self._snapshot_save_timer.setInterval(15000)
        self._snapshot_save_timer.timeout.connect(self._save_snapshot_async)

        # --- Создание "скелета" UI ---
        self._init_lazy_ui_skeleton()

        # --- Подключение к глобальным событиям ---
        app.event_bus.event_posted.connect(self.on_event)


    def _apply_initial_geometry(self):
        """Задает стартовый размер и позицию до первого показа окна."""
        screen = self.screen()
        if screen is None:
            app = QtWidgets.QApplication.instance()
            screen = app.primaryScreen() if app else None
        if screen is None:
            return

        available_geometry = screen.availableGeometry()
        width = int(self.minimumWidth() * 1.6)
        width = min(width, int(available_geometry.width() * 0.92))
        height = int(available_geometry.height() * 0.88)
        height = min(height, int(available_geometry.height() * 0.92))

        # Делаем первичную геометрию заранее, чтобы окно не "отскакивало",
        # если пользователь начинает перетаскивать его сразу после запуска.
        self.resize(width, height)
        self.move(
            available_geometry.center().x() - self.width() // 2,
            available_geometry.center().y() - self.height() // 2
        )


    def _populate_full_ui(self):
        """
        Создает и размещает все "тяжелые" виджеты.
        Версия 3.0: Объединенная вкладка 'Настройки' (Ключи + Модель).
        """
        content_layout = QVBoxLayout(self.main_content_widget)
        content_layout.setContentsMargins(10, 10, 10, 0)
        content_layout.setSpacing(8)

        # --- ШАГ 1: СОЗДАЕМ ВСЕ КАСТОМНЫЕ ВИДЖЕТЫ-КОМПОНЕНТЫ ---
        self.paths_widget = ProjectPathsWidget(self)
        self.task_management_widget = TaskManagementWidget(self)
        self.log_widget = LogWidget(self)
        self.glossary_widget = GlossaryWidget(self, settings_manager=self.settings_manager)

        self.preset_widget = PresetWidget(
            parent=self, preset_name="Промпт", default_prompt_func=api_config.default_prompt,
            load_presets_func=self.settings_manager.load_named_prompts,
            save_presets_func=self.settings_manager.save_named_prompts,
            get_last_text_func=self.settings_manager.get_custom_prompt,
            get_last_preset_func=self.settings_manager.get_last_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_prompt_preset_name,
            builtin_presets_func=api_config.builtin_translation_prompt_variants
        )
        self.preset_widget.load_last_session_state()

        self.translation_options_widget = TranslationOptionsWidget(self)
        server_manager = self.app.get_server_manager() if hasattr(self.app, 'get_server_manager') else None
        self.model_settings_widget = ModelSettingsWidget(self, settings_manager=self.settings_manager, server_manager=server_manager)
        self.manual_translation_widget = ManualTranslationWidget(
            self,
            settings_manager=self.settings_manager,
            model_settings_widget=self.model_settings_widget,
            settings_getter=self.get_settings
        )
        self.auto_translate_widget = AutoTranslateWidget(
            self,
            settings_manager=self.settings_manager,
        )
        self.project_actions_widget = ProjectActionsWidget(self)
        self.status_bar = StatusBarWidget(self, event_bus=self.bus, engine=self.engine)

        # --- ШАГ 2: СОЗДАЕМ ОБЪЕДИНЕННУЮ ВКЛАДКУ "НАСТРОЙКИ" ---
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        settings_layout.setContentsMargins(4, 4, 4, 4)
        settings_layout.setSpacing(8)

        # 2.1. Группа Ключей и Распределения (Верхняя часть)
        # Сначала создаем виджет распределения, который внедрится в KeyManagementWidget
        distribution_group = QGroupBox("Параллельная обработка")
        dist_controls_layout = QHBoxLayout(distribution_group)
        dist_controls_layout.addWidget(QLabel("Обработчиков:  "))

        self.instances_spin = NoScrollSpinBox()
        self.instances_spin.setRange(1, 1)
        self.instances_spin.setToolTip(
            "Количество параллельных обработчиков для одновременного перевода глав.\n"
            "Каждый обработчик использует одну активную сессию сервиса или браузерный профиль.\n"
            "Увеличение этого значения ускоряет перевод, если выбранный сервис поддерживает несколько параллельных сессий."
        )
        self.instances_spin.valueChanged.connect(self._update_distribution_info_from_widget)
        dist_controls_layout.addWidget(self.instances_spin)
        dist_controls_layout.addStretch()

        self.distribution_label = QLabel("…")
        self.distribution_label.setStyleSheet("color: #90EE90; font-size: 10pt; font-weight: bold;")
        dist_controls_layout.addWidget(self.distribution_label)

        # Теперь создаем сам KeyManagementWidget
        server_manager = self.app.get_server_manager() if hasattr(self.app, 'get_server_manager') else None
        self.key_management_widget = KeyManagementWidget(
            self.settings_manager,
            parent=self,
            distribution_group_widget=distribution_group,
            server_manager=server_manager
        )
        # Подключаем сигналы ключей
        self.key_management_widget.active_keys_changed.connect(self._update_distribution_info_from_widget)
        self.key_management_widget.active_keys_changed.connect(self.check_ready)

        # Оборачиваем в группу для визуальной целостности
        keys_container_group = QGroupBox("Сервисы, сессии и распределение нагрузки")
        keys_container_layout = QVBoxLayout(keys_container_group)
        keys_container_layout.setContentsMargins(2, 8, 2, 2)
        keys_container_layout.addWidget(self.key_management_widget)

        # Добавляем группу ключей наверх (stretch=1, чтобы она занимала все свободное место)
        settings_layout.addWidget(keys_container_group, 1)

        # 2.2. Группа Настроек Модели (Нижняя часть)
        # model_settings_widget уже является QGroupBox, просто добавляем его
        # stretch=0, чтобы она занимала только необходимый минимум высоты
        settings_layout.addWidget(self.model_settings_widget, 0)
        self.appearance_group = self._create_appearance_group()
        settings_layout.addWidget(self.appearance_group, 0)
        self.model_settings_widget.prettify_checkbox.setVisible(True)
        # --- ШАГ 3: СОБИРАЕМ QTabWidget ---
        self.tabs_group = QTabWidget()
        self.tabs_group.setDocumentMode(True)
        tabs_group = self.tabs_group

        # Вкладка 1: Настройки (Объединенная)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        settings_scroll.setWidget(settings_tab)
        tabs_group.addTab(settings_scroll, "Настройки")

        # Вкладка 2: Список Задач + Оптимизация
        tasks_tab_container = QWidget()
        tasks_tab_layout = QVBoxLayout(tasks_tab_container)
        tasks_tab_layout.setContentsMargins(4, 4, 4, 4)
        tasks_tab_layout.setSpacing(8)
        self.tasks_splitter = QSplitter(QtCore.Qt.Orientation.Vertical)
        self.tasks_splitter.setChildrenCollapsible(False)
        self.tasks_splitter.addWidget(self.task_management_widget)
        self.tasks_splitter.addWidget(self.translation_options_widget)
        self.tasks_splitter.setStretchFactor(0, 5)
        self.tasks_splitter.setStretchFactor(1, 1)
        self.tasks_splitter.setCollapsible(0, False)
        self.tasks_splitter.setCollapsible(1, True)
        self.tasks_splitter.setSizes([560, 150])
        tasks_tab_layout.addWidget(self.tasks_splitter, 1)
        tabs_group.addTab(tasks_tab_container, "Список Задач")

        # Остальные вкладки
        tabs_group.addTab(self.log_widget, "Логирование")
        self.glossary_tab_index = tabs_group.addTab(self.glossary_widget, "Глоссарий")
        tabs_group.addTab(self.preset_widget, "Промпт")
        tabs_group.addTab(self.manual_translation_widget, "Ручной перевод")
        self.auto_translate_tab_index = tabs_group.addTab(self.auto_translate_widget, "Автоперевод")
        tabs_group.currentChanged.connect(self._on_main_tab_changed)

        # --- ШАГ 4: КОМПОНОВКА ОСНОВНОГО ОКНА ---
        content_layout.addWidget(self.paths_widget)
        content_layout.addWidget(tabs_group, 1)

        # Нижняя панель с кнопками
        action_bar = QFrame(self.main_content_widget)
        action_bar.setObjectName("actionBar")
        bottom_panel_layout = QHBoxLayout(action_bar)
        bottom_panel_layout.setContentsMargins(10, 8, 10, 8)
        bottom_panel_layout.setSpacing(10)

        self.use_project_settings_btn = QtWidgets.QPushButton("Глобальные настройки")
        self.use_project_settings_btn.setObjectName("contextToggleButton")
        self.use_project_settings_btn.setCheckable(True)
        self.use_project_settings_btn.setChecked(False)
        self.use_project_settings_btn.setVisible(False)

        self.start_btn = QPushButton("Старт перевода")
        self.start_btn.setObjectName("primaryActionButton")
        self.start_btn.setMinimumHeight(36)
        self.stop_btn = QPushButton("Плавный стоп")
        self.stop_btn.setObjectName("dangerActionButton")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setEnabled(False)
        self.dry_run_btn = QPushButton("Пробный запуск")
        self.dry_run_btn.setObjectName("compactActionButton")
        self.dry_run_btn.setMinimumHeight(36)
        self.close_btn = QPushButton("В меню")
        self.close_btn.setObjectName("ghostActionButton")
        self.close_btn.setMinimumHeight(36)
        self._set_stop_button_mode(False)

        bottom_panel_layout.addWidget(self.project_actions_widget, 1)

        if self._translator_only_mode:
            self.proxy_status_label = QLabel("Прокси: выключен")
            self.proxy_status_label.setObjectName("helperLabel")
            self.proxy_status_label.setToolTip("Сетевые запросы идут без прокси.")
            bottom_panel_layout.addWidget(self.proxy_status_label, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

            self.proxy_button = QPushButton("Прокси")
            self.proxy_button.setObjectName("compactActionButton")
            self.proxy_button.setMinimumHeight(36)
            bottom_panel_layout.addWidget(self.proxy_button)

        bottom_panel_layout.addWidget(self.use_project_settings_btn)

        right_buttons_layout = QHBoxLayout()
        right_buttons_layout.setSpacing(8)
        right_buttons_layout.addStretch()
        right_buttons_layout.addWidget(self.dry_run_btn)
        right_buttons_layout.addWidget(self.start_btn)
        right_buttons_layout.addWidget(self.stop_btn)
        right_buttons_layout.addWidget(self.close_btn)

        bottom_panel_layout.addLayout(right_buttons_layout)
        content_layout.addWidget(action_bar)

        content_layout.addWidget(self.status_bar)

        self._connect_signals()
        if self._translator_only_mode:
            self._activate_proxy_from_settings()
        self.check_ready()

    def _connect_signals(self):
        """Подключает все сигналы и слоты для виджетов диалога."""
        self.use_project_settings_btn.toggled.connect(self._toggle_project_settings_mode)
        self.paths_widget.file_selected.connect(self.on_file_selected)
        self.paths_widget.folder_selected.connect(self.on_folder_selected)
        self.paths_widget.chapters_reselection_requested.connect(self.reselect_chapters)
        self.paths_widget.swap_file_requested.connect(self._on_swap_file_requested)
        self.project_actions_widget.open_history_requested.connect(self._open_project_history)
        self.project_actions_widget.sync_project_requested.connect(self._run_project_sync)

        self.translation_options_widget.settings_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=False))
        self.translation_options_widget.task_size_spin.valueChanged.connect(self._refresh_auto_translate_runtime_context)
        self.task_management_widget.tasks_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=True))

        self.model_settings_widget.recalibrate_requested.connect(self._calibrate_cpu)
        self.model_settings_widget.model_combo.currentIndexChanged.connect(self._refresh_auto_translate_runtime_context)
        self.model_settings_widget.settings_changed.connect(self._refresh_auto_translate_runtime_context)
        self.key_management_widget.active_keys_changed.connect(self._update_instances_spinbox_limit)
        self.key_management_widget.active_keys_changed.connect(self.check_ready)
        self.key_management_widget.provider_combo.currentIndexChanged.connect(self._update_instances_spinbox_limit)
        self.key_management_widget.provider_combo.currentIndexChanged.connect(self.check_ready)
        self.key_management_widget.provider_combo.currentIndexChanged.connect(self._refresh_auto_translate_runtime_context)

        # --- ИЕРАРХИЯ Подключаемся только к TaskManagementWidget ---
        self.task_management_widget.tasks_changed.connect(lambda: self._prepare_and_display_tasks(clean_rebuild=True))
        self.task_management_widget.reorder_requested.connect(self._handle_task_reorder)
        self.task_management_widget.duplicate_requested.connect(self._handle_task_duplication)
        self.task_management_widget.remove_selected_requested.connect(self._handle_task_removal)
        self.task_management_widget.copy_originals_requested.connect(self._copy_original_chapters)
        self.task_management_widget.reanimate_requested.connect(self._handle_task_reanimation)
        self.task_management_widget.split_batch_requested.connect(self._handle_batch_split)
        self.task_management_widget.batch_chapters_reorder_requested.connect(self._handle_batch_chapter_reorder)
        self.task_management_widget.chapter_preview_requested.connect(self._open_chapter_preview_from_queue)
        self.task_management_widget.filter_all_translated_requested.connect(self._filter_all_translated_tasks)
        self.task_management_widget.filter_validated_requested.connect(self._filter_validated_tasks)
        self.task_management_widget.filter_packaging_requested.connect(self._open_filter_packaging_dialog)
        self.task_management_widget.validation_requested.connect(self.open_translation_validator)
        self.task_management_widget.backup_restore_requested.connect(self._handle_backup_restore)
        # --------------------------------------------------------------------------

        self.start_btn.clicked.connect(self._start_translation)
        self.stop_btn.clicked.connect(self._stop_translation)
        self.dry_run_btn.clicked.connect(self.perform_dry_run)
        self.close_btn.clicked.connect(self._return_to_main_menu_from_button)
        if self.proxy_button is not None:
            self.proxy_button.clicked.connect(self._open_proxy_settings)
        self.project_actions_widget.build_epub_requested.connect(self._open_epub_builder_standalone)

        self.model_settings_widget.settings_changed.connect(self._mark_settings_as_dirty)
        self.translation_options_widget.settings_changed.connect(self._mark_settings_as_dirty)
        self.key_management_widget.active_keys_changed.connect(self._mark_settings_as_dirty)
        self.key_management_widget.provider_combo.currentIndexChanged.connect(self._mark_settings_as_dirty)
        self.instances_spin.valueChanged.connect(self._mark_settings_as_dirty)
        self.preset_widget.text_changed.connect(self._mark_promt_as_dirty)
        self.glossary_widget.glossary_changed.connect(self._on_glossary_changed)
        self.auto_translate_widget.settings_changed.connect(self._mark_settings_as_dirty)
        self.auto_translate_widget.open_glossary_requested.connect(self.open_ai_glossary_generation)
        self.auto_translate_widget.open_validator_requested.connect(self.open_translation_validator)
        self.auto_translate_widget.open_consistency_requested.connect(self.open_ai_consistency_checker)
        self._refresh_auto_translate_runtime_context()

    def _on_main_tab_changed(self, index: int):
        if index == getattr(self, 'glossary_tab_index', -1):
            QtCore.QTimer.singleShot(0, self._maybe_offer_base_glossaries_for_empty_project)
        if index == getattr(self, 'auto_translate_tab_index', -1):
            QtCore.QTimer.singleShot(0, self.auto_translate_widget.refresh_glossary_presets)

    def _base_glossary_state_path(self):
        if not self.output_folder:
            return None
        return os.path.join(self.output_folder, BASE_GLOSSARY_PROMPT_STATE_FILE)

    def _load_base_glossary_prompt_state(self) -> dict:
        state_path = self._base_glossary_state_path()
        if not state_path or not os.path.exists(state_path):
            return {}
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_base_glossary_prompt_state(self, selected_ids=None, skipped=False):
        state_path = self._base_glossary_state_path()
        if not state_path:
            return
        data = {
            "prompted": True,
            "selected_ids": selected_ids or [],
            "skipped": bool(skipped),
            "timestamp": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception as e:
            print(f"[WARN] Не удалось сохранить состояние выбора базового глоссария: {e}")

    def _get_available_base_glossary_options(self) -> list:
        options = []
        for glossary_id, display_name in api_config.base_glossary_names().items():
            entries = api_config.load_base_glossary(glossary_id)
            if entries:
                options.append({
                    "id": glossary_id,
                    "name": display_name,
                    "count": len(entries),
                })
        return options

    def _merge_base_glossary_into_project_glossary(self, glossary_id: str) -> int:
        base_glossary = api_config.load_base_glossary(glossary_id)
        if not base_glossary:
            return 0

        current_glossary = self.glossary_widget.get_glossary()
        existing_keys = {
            str(item.get('original', '')).lower().strip()
            for item in current_glossary
            if item.get('original')
        }

        merged_glossary = list(current_glossary)
        now = time.time()
        added_count = 0
        for item in base_glossary:
            original = str(item.get('original', '')).strip()
            rus = str(item.get('rus', '')).strip()
            if not original or not rus:
                continue

            key = original.lower()
            if key in existing_keys:
                continue

            merged_glossary.append({
                "original": original,
                "rus": rus,
                "note": str(item.get('note', '')).strip(),
                "timestamp": item.get('timestamp', now),
            })
            existing_keys.add(key)
            added_count += 1

        if added_count:
            self.glossary_widget.set_glossary(merged_glossary)

        return added_count

    def _maybe_offer_base_glossaries_for_empty_project(self):
        if not self.output_folder or not hasattr(self, 'glossary_widget'):
            return
        if self.glossary_widget.get_glossary():
            return

        project_key = os.path.abspath(self.output_folder)
        if project_key in self._base_glossary_prompt_seen_projects:
            return

        state = self._load_base_glossary_prompt_state()
        if state.get("prompted"):
            self._base_glossary_prompt_seen_projects.add(project_key)
            return

        options = self._get_available_base_glossary_options()
        if not options:
            return

        self._base_glossary_prompt_seen_projects.add(project_key)
        dialog = BaseGlossarySelectionDialog(options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_ids = dialog.selected_ids()
        if dialog.skipped() or not selected_ids:
            self._save_base_glossary_prompt_state(selected_ids=[], skipped=True)
            return

        settings_dirty_before = self.is_settings_dirty
        added_total = 0
        for glossary_id in selected_ids:
            added_total += self._merge_base_glossary_into_project_glossary(glossary_id)

        self._save_base_glossary_prompt_state(selected_ids=selected_ids, skipped=False)
        if added_total:
            self._save_project_glossary_only()
            if not settings_dirty_before:
                self.is_settings_dirty = False
                self._refresh_dirty_window_title()
            QMessageBox.information(self, "Глоссарий добавлен", f"Добавлено записей: {added_total}.")

    def _create_glossary_tab_content(self) -> QWidget:
        """Просто возвращает уже созданный GlossaryWidget."""
        return self.glossary_widget

    def _create_prompt_tab_content(self) -> QWidget:
        """Просто возвращает уже созданный PresetWidget."""
        return self.preset_widget

    def _prepare_for_close(self):
        """Обрабатывает несохраненные изменения перед закрытием окна."""
        has_unsaved_settings = self.is_settings_dirty

        has_unsaved_glossary = (
            self.output_folder
            and (self.is_glossary_dirty or self.glossary_widget.get_glossary() != self.initial_glossary_state)
        )

        should_show_dialog = has_unsaved_settings or has_unsaved_glossary
        user_choice_to_exit = True
        skip_global_save_on_exit = False

        if should_show_dialog:
            is_local_mode = self.local_set and self.output_folder

            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Несохраненные изменения")
            msg_box.setIcon(QMessageBox.Icon.Question)

            messages = []
            if has_unsaved_settings:
                messages.append("настройки сессии")
            if has_unsaved_glossary:
                messages.append("глоссарий")
            msg_box.setText(f"Обнаружены несохраненные изменения: {', '.join(messages)}.")

            if is_local_mode:
                msg_box.setInformativeText("Сохранить все изменения в файлы текущего проекта?")
                save_btn = msg_box.addButton("Сохранить в Проект", QMessageBox.ButtonRole.AcceptRole)
            else:
                msg_box.setInformativeText("Выберите действие для сохранения.")
                save_btn = msg_box.addButton("Сохранить изменения", QMessageBox.ButtonRole.AcceptRole)

            msg_box.addButton("Выйти без сохранения", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)

            msg_box.exec()
            clicked_button = msg_box.clickedButton()

            if clicked_button == save_btn:
                if is_local_mode:
                    if has_unsaved_settings:
                        self._save_project_settings_only()
                    if has_unsaved_glossary:
                        self._save_project_glossary_only()
                elif has_unsaved_glossary:
                    self._save_project_glossary_only()
            elif clicked_button == cancel_btn:
                user_choice_to_exit = False
            elif not is_local_mode:
                skip_global_save_on_exit = True

        if not user_choice_to_exit:
            return False

        self.settings_manager.save_custom_prompt(self.preset_widget.get_prompt())
        self.settings_manager.save_last_prompt_preset_name(self.preset_widget.get_current_preset_name())

        if not (self.local_set and self.output_folder) and not skip_global_save_on_exit:
            self._save_global_ui_settings(clear_dirty=False)

        return True

    def _return_to_main_menu_from_button(self):
        """Возвращает пользователя в главное меню по кнопке 'Выход'."""
        if not self._prepare_for_close():
            return
        self._returning_to_main_menu = True
        self.close()

    def _set_stop_button_mode(self, hard_stop: bool):
        self._hard_stop_enabled = hard_stop
        if hard_stop:
            self.stop_btn.setText("Экстренный стоп")
            self.stop_btn.setToolTip("Немедленно остановить сессию.")
        else:
            self.stop_btn.setText("Плавный стоп")
            self.stop_btn.setToolTip("Не брать новые задачи и дождаться завершения уже взятых.")

    def _load_initial_data(self):
        """
        Выполняет всю долгую инициализацию виджетов после того,
        как окно было показано.
        """
        print("[DEBUG] Запуск отложенной загрузки данных для InitialSetupDialog…")

        # 1. Первоначальная синхронизация провайдера и ключей.
        #    Это может читать с диска, поэтому делаем это здесь.
        self.key_management_widget.provider_combo.currentIndexChanged.emit(
            self.key_management_widget.provider_combo.currentIndex()
        )

        self._restore_global_ui_settings()

        # 3. Проверяем, нужно ли автозаполнение из валидатора
        if self.prefill_data and self.prefill_data.get("is_restarting"):
            self.autofill_from_validator()

        # 4. Финальная проверка состояния кнопок после загрузки всех данных
        self.check_ready()
        print("[DEBUG] Отложенная загрузка данных для InitialSetupDialog завершена.")

    # --------------------------------------------------------------------
    # МЕТОДЫ СОЗДАНИЯ ЭЛЕМЕНТОВ UI
    # --------------------------------------------------------------------

    def _create_appearance_group(self) -> QGroupBox:
        group = QGroupBox("Внешний вид интерфейса")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)

        hint_label = QLabel(
            "Можно менять основные цвета интерфейса. Изменения применяются сразу ко всему приложению. "
            "Для фона и панелей лучше подходят тёмные оттенки."
        )
        hint_label.setWordWrap(True)
        hint_label.setObjectName("helperLabel")
        layout.addWidget(hint_label)

        form_layout = QFormLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)

        color_fields = {
            "window_bg": "Фон окна",
            "panel_bg": "Фон панелей",
            "accent": "Акцент",
        }

        for color_key, label_text in color_fields.items():
            button = QPushButton()
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, key=color_key: self._choose_ui_theme_color(key))
            self.theme_color_buttons[color_key] = button
            form_layout.addRow(label_text, button)

        layout.addLayout(form_layout)

        actions_layout = QHBoxLayout()
        actions_layout.addStretch(1)
        reset_button = QPushButton("Сбросить цвета")
        reset_button.clicked.connect(self._reset_ui_theme_colors)
        actions_layout.addWidget(reset_button)
        layout.addLayout(actions_layout)

        self._refresh_ui_theme_controls()
        return group

    def _refresh_ui_theme_controls(self):
        if not getattr(self, "theme_color_buttons", None):
            return

        colors = editable_theme_colors(getattr(self, "_ui_theme_colors", None))
        captions = {
            "window_bg": "Фон окна",
            "panel_bg": "Фон панелей",
            "accent": "Акцент",
        }

        for color_key, button in self.theme_color_buttons.items():
            color_value = colors[color_key]
            qcolor = QtGui.QColor(color_value)
            text_color = "#10161d" if qcolor.lightnessF() > 0.62 else "#ffffff"
            border_color = qcolor.darker(145).name() if qcolor.lightnessF() > 0.62 else qcolor.lighter(145).name()
            button.setText(color_value.upper())
            button.setToolTip(f"{captions.get(color_key, color_key)}: {color_value.upper()}")
            button.setStyleSheet(
                "QPushButton {"
                f"background-color: {color_value};"
                f"color: {text_color};"
                f"border: 1px solid {border_color};"
                "font-weight: 600;"
                "padding: 6px 10px;"
                "text-align: left;"
                "}"
            )

    def _apply_ui_theme_colors(self, theme_colors=None, mark_dirty=False):
        next_colors = editable_theme_colors(theme_colors)
        current_colors = editable_theme_colors(getattr(self, "_ui_theme_colors", None))
        has_changed = next_colors != current_colors

        self._ui_theme_colors = dict(next_colors)

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_dark_stylesheet(self._ui_theme_colors))

        self._refresh_ui_theme_controls()

        if mark_dirty and has_changed:
            self._mark_settings_as_dirty()

    def _choose_ui_theme_color(self, color_key: str):
        colors = editable_theme_colors(getattr(self, "_ui_theme_colors", None))
        current_color = QtGui.QColor(colors.get(color_key, "#000000"))
        selected_color = QColorDialog.getColor(
            current_color,
            self,
            "Выберите цвет интерфейса",
        )
        if not selected_color.isValid():
            return

        colors[color_key] = selected_color.name()
        self._apply_ui_theme_colors(colors, mark_dirty=True)

    def _reset_ui_theme_colors(self):
        self._apply_ui_theme_colors({}, mark_dirty=True)

    def _get_available_session_capacity(self) -> int:
        provider_id = self.key_management_widget.get_selected_provider()
        active_sessions = len(self.key_management_widget.get_active_keys())
        if active_sessions <= 0:
            return 0
        provider_limit = api_config.provider_max_instances(provider_id)
        if provider_limit is None or provider_limit <= 0:
            provider_limit = active_sessions
        return min(active_sessions, provider_limit)

    def _update_distribution_info_from_widget(self):
        num_chapters = len(self.html_files)
        if num_chapters == 0:
            self.distribution_label.setText("…")
            self.distribution_label.setStyleSheet("color: grey;")
            return

        session_capacity = self._get_available_session_capacity()
        self.instances_spin.setMaximum(session_capacity if session_capacity > 0 else 1)

        num_instances = self.instances_spin.value()

        if session_capacity == 0 or num_instances == 0:
            self.distribution_label.setText("Нет активной сессии")
            self.distribution_label.setStyleSheet("color: orange; font-weight: bold;")
            return

        if num_instances > num_chapters:
            self.distribution_label.setText(f"Клиентов ({num_instances}) > глав ({num_chapters})")
            self.distribution_label.setStyleSheet("color: orange; font-weight: bold;")
            return

        # Расчет среднего с округлением вверх
        avg_chapters = math.ceil(num_chapters / num_instances)

        text = f"≈ {avg_chapters} глав / обработчик"
        self.distribution_label.setText(text)
        self.distribution_label.setStyleSheet("color: #90EE90; font-size: 10pt; font-weight: bold;")

    def _post_event(self, name: str, data: dict = None):
        session_id = self.engine.session_id if self.engine and self.engine.session_id else None
        event = {
            'event': name,
            'source': 'InitialSetupDialog',
            'session_id': session_id,
            'data': data or {}
        }
        self.bus.event_posted.emit(event)

    def _handle_geoblock_detected(self):
        """
        Показывает пользователю кастомный, терапевтический диалог о геоблокировке,
        который не пугает, а предлагает решение.
        """
        # Просто создаем и запускаем наш новый, умный диалог.
        dialog = GeoBlockDialog(self)
        dialog.exec()

    def create_glossary_tab(self, tabs_group):
        # 1. Создаем экземпляр нашего виджета, передавая ему settings_manager
        self.glossary_widget = GlossaryWidget(self, settings_manager=self.settings_manager)
        self.glossary_widget.set_project_path(self.output_folder)

        # 3. Добавляем его как вкладку
        tabs_group.addTab(self.glossary_widget, "Глоссарий и Контекст Проекта")


    def save_ui_state(self, ui_state_dict):
        """
        Загружает текущие настройки, обновляет их значениями из UI
        и сохраняет обратно в файл. Это безопасный способ обновить
        только те настройки, которыми управляет UI.
        """
        with self.file_lock:
            settings = self.load_settings()

            # Обновляем только те ключи, которые приходят из UI
            # (используем префикс 'last_', как в save_last_settings)
            settings['last_model'] = ui_state_dict.get('model')
            settings['last_temperature'] = ui_state_dict.get('temperature')
            settings['last_temperature_override_enabled'] = ui_state_dict.get('temperature_override_enabled', False)
            settings['last_concurrent_requests'] = ui_state_dict.get('rpm_limit')
            settings['last_chunking'] = ui_state_dict.get('chunking')
            settings['last_dynamic_glossary'] = ui_state_dict.get('dynamic_glossary')
            settings['last_system_instruction'] = ui_state_dict.get('use_system_instruction')
            settings['last_thinking_enabled'] = ui_state_dict.get('thinking_enabled')
            settings['last_thinking_budget'] = ui_state_dict.get('thinking_budget')
            settings['last_use_json_epub_pipeline'] = ui_state_dict.get('use_json_epub_pipeline')

            # Также сохраняем последние использованные пресеты
            if 'last_prompt_preset' in ui_state_dict:
                settings['last_prompt_preset'] = ui_state_dict['last_prompt_preset']
            if 'custom_prompt' in ui_state_dict:
                settings['custom_prompt'] = ui_state_dict['custom_prompt']

            # Сохраняем обновленный словарь
            return self.save_settings(settings)

    def create_prompt_tab(self, tabs_group):
        # 1. Создаем экземпляр нашего виджета с полной конфигурацией
        self.preset_widget = PresetWidget(
            parent=self,
            preset_name="Промпт",
            default_prompt_func=api_config.default_prompt,
            load_presets_func=self.settings_manager.load_named_prompts,
            save_presets_func=self.settings_manager.save_named_prompts,
            get_last_text_func=self.settings_manager.get_custom_prompt,
            get_last_preset_func=self.settings_manager.get_last_prompt_preset_name,
            save_last_preset_func=self.settings_manager.save_last_prompt_preset_name,
            builtin_presets_func=api_config.builtin_translation_prompt_variants
        )
        self.preset_widget.load_last_session_state()
        # 3. Добавляем его как вкладку
        tabs_group.addTab(self.preset_widget, "Промпт (опционально)")


    def _update_recommendations(self):
        """
        Централизованно обновляет рекомендации по размеру задачи.
        Берет модель из виджета моделей и передает в виджет опций.
        """
        if not self.model_settings_widget or not self.translation_options_widget:
            return

        model_name = self.model_settings_widget.model_combo.currentText()
        self.translation_options_widget.update_recommendations_from_model(model_name)
        self._refresh_auto_translate_runtime_context()

    def _refresh_auto_translate_runtime_context(self):
        if not hasattr(self, 'auto_translate_widget'):
            return
        if not hasattr(self, 'translation_options_widget') or not hasattr(self, 'model_settings_widget'):
            return
        if not hasattr(self, 'key_management_widget'):
            return

        chapter_compositions = getattr(self.translation_options_widget, 'chapter_compositions', {}) or {}
        uses_cjk = any(
            isinstance(composition, dict) and composition.get('is_cjk')
            for composition in chapter_compositions.values()
        )
        self.auto_translate_widget.set_runtime_context(
            provider_id=self.key_management_widget.get_selected_provider(),
            current_model_name=self.model_settings_widget.model_combo.currentText(),
            current_task_size_limit=self.translation_options_widget.task_size_spin.value(),
            uses_cjk=uses_cjk,
            current_model_settings=self.model_settings_widget.get_settings(),
        )


    def _update_distribution_info(self):
        num_chapters = len(self.html_files)
        if num_chapters == 0: self.distribution_label.setText("Сначала выберите главы."); return
        num_instances = self.instances_spin.value()
        if num_instances > num_chapters: self.distribution_label.setText(f"<font color='orange'><b>Предупреждение:</b> Обработчиков ({num_instances}) больше, чем заданий ({num_chapters}).</font>"); return
        base, extra = num_chapters // num_instances, num_chapters % num_instances

        avg_chapters = math.ceil(num_chapters / num_instances)

        text = f"≈ {avg_chapters} глав / обработчик"
        self.distribution_label.setText(text)


    # ЗАМЕНИТЕ ЭТОТ МЕТОД
    def _calculate_potential_output_size(self, html_content, is_cjk):
        """
        Вычисляет потенциальный размер ответа модели на основе содержимого HTML.
        Устаревший метод, используйте глобальную функцию calculate_potential_output_size.
        """
        return calculate_potential_output_size(html_content, is_cjk)

    # --------------------------------------------------------------------
    # ОБЩАЯ ЛОГИКА И ОБРАБОТЧИКИ
    # --------------------------------------------------------------------

    def autofill_from_validator(self):
        """Заполняет поля данными, полученными из валидатора."""
        if not self.prefill_data: return

        epub_path = self.prefill_data.get("epub_path")
        chapters = self.prefill_data.get("chapters")

        if epub_path and chapters:
            self.selected_file = epub_path

            self.paths_widget.set_file_path(epub_path)


            self._process_selected_file(pre_selected_chapters=chapters)

            if not self.output_folder:
                self.output_folder = os.path.dirname(epub_path)

                self.paths_widget.set_folder_path(self.output_folder)


    @pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """
        Обрабатывает только те события, которые касаются самого диалога,
        а не его дочерних виджетов.
        """
        event_name = event_data.get('event')
        data = event_data.get('data', {})

        if self.is_blocked_by_child_dialog and event_name != 'tasks_for_retry_ready':
            return

        if event_name == 'current_proxy_status':
            self._update_proxy_display(data)
            return

        # Этот виджет теперь реагирует только на старт и финиш сессии
        if event_name == 'session_started':
            self.is_session_active = True
            # total_tasks теперь обрабатывается в StatusBarWidget
            self._set_controls_enabled(False)
            self._save_snapshot_async(force=True)
            return
        if event_name == 'assembly_finished' and self.is_session_active == False:
            if self.project_manager:
                self.project_manager.reload_data_from_disk()

        if event_name == 'session_finished':
            self._shutdown_reason = data.get('reason')
            self._log_session_id = data.get('session_id_log')
            QtCore.QMetaObject.invokeMethod(
                self, "_on_session_finished",
                QtCore.Qt.ConnectionType.QueuedConnection
            )
            self.this_dialog_started_the_session = False
            return

        if event_name == 'tasks_for_retry_ready':
            epub_path, chapter_paths = data.get('epub_path'), data.get('chapter_paths')
            if epub_path and chapter_paths: self.add_files_for_retry(epub_path, chapter_paths)
            return

        if event_name == 'task_state_changed':
            self._schedule_snapshot_save()
            return

        # Логика для geoblock остается здесь, так как она показывает модальное окно
        if self.is_session_active and event_name == 'geoblock_detected':
            self._handle_geoblock_detected()

    def _open_proxy_settings(self):
        from .proxy import ProxySettingsDialog

        dialog = ProxySettingsDialog(self, self.settings_manager)
        dialog.exec()

    def _update_proxy_display(self, settings):
        label = getattr(self, 'proxy_status_label', None)
        if label is None:
            return

        enabled = settings.get('enabled', False)
        proxy_type = str(settings.get('type', 'SOCKS5'))
        host = str(settings.get('host') or 'не настроен')
        port = str(settings.get('port') or '')
        user = str(settings.get('user') or '')

        if enabled and host != 'не настроен' and port:
            label.setText(f"Прокси: {proxy_type}://{host}:{port}")
            tooltip_lines = [f"Тип: {proxy_type}", f"Хост: {host}", f"Порт: {port}"]
            if user:
                tooltip_lines.append(f"Пользователь: {user}")
            label.setToolTip("\n".join(tooltip_lines))
            label.setStyleSheet("color: #4CAF50;")
        else:
            label.setText("Прокси: выключен")
            label.setToolTip("Сетевые запросы идут без прокси.")
            label.setStyleSheet("color: #9aa4b2;")

    def _activate_proxy_from_settings(self):
        if self.proxy_status_label is None:
            return

        settings = self.settings_manager.load_proxy_settings()
        self.bus.event_posted.emit({
            'event': 'proxy_started',
            'source': 'InitialSetupDialog',
            'data': settings,
        })

    def reselect_chapters(self):
        """
        Повторно открывает диалог выбора глав для уже выбранного файла.
        Вызывается при нажатии на кнопку со счетчиком глав.
        """
        if not self.selected_file:
            # Эта проверка на всякий случай, если кнопка будет видна, когда не должна
            QMessageBox.warning(self, "Ошибка", "Сначала выберите EPUB файл.")
            return

        # --- НОВЫЙ БЛОК: Принудительная синхронизация ---
        if self.project_manager:
            self.project_manager.reload_data_from_disk()
            print("[INFO] Карта проекта принудительно обновлена перед выбором глав.")
        # --- КОНЕЦ НОВОГО БЛОКА ---
        self._process_selected_file()


    def _process_selected_file(self, pre_selected_chapters=None):
        """
        Главная функция для работы с EPUB. Финальная версия с правильной последовательностью.
        """
        if not self.selected_file or not os.path.exists(self.selected_file):
            return
        if self.task_manager:
            self.task_manager.clear_glossary_results()
        try:
            success, selected_files = EpubHtmlSelectorDialog.get_selection(
                parent=self,
                epub_filename=self.selected_file,
                output_folder=self.output_folder,
                pre_selected_chapters=pre_selected_chapters if pre_selected_chapters is not None else self.html_files,
                project_manager=self.project_manager
            )

            if success:
                self.html_files = selected_files
                self.paths_widget.update_chapters_info(len(self.html_files))

                if self.output_folder:
                    self._handle_project_initialization()
                else:
                    self._prepare_and_display_tasks(clean_rebuild=True)

        except Exception as e:
            # --- БЛОК НА ЗАМЕНУ ---
            tb_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            # --- ИЗМЕНЕНИЕ: Форматируем сообщение с двойным переносом строки ---
            error_message = (
                f"Не удалось проанализировать файл '{os.path.basename(self.selected_file)}'.\n\n" # <--- Основной текст
                f"--- Полный Traceback ---\n{tb_str}" # <--- Детали
            )
            print(f"[ERROR] Локальная ошибка в _process_selected_file:\n{error_message}")

            # Просто вызываем наш "патченный" метод
            QtWidgets.QMessageBox.critical(self, "Ошибка обработки EPUB", error_message)
            # --- КОНЕЦ БЛОКА ---
            self.selected_file = None
            self.html_files = []
            self.paths_widget.set_file_path(None)
            self.check_ready()

    def _refresh_dirty_window_title(self):
        clean_title = self.windowTitle().replace("*", "")
        if self.is_settings_dirty or self.is_glossary_dirty:
            self.setWindowTitle(clean_title + "*")
        else:
            self.setWindowTitle(clean_title)

    def mark_project_glossary_as_saved(self, glossary_data=None):
        if glossary_data is None:
            glossary_data = self.glossary_widget.get_glossary() if hasattr(self, "glossary_widget") else []

        self.initial_glossary_state = [item.copy() for item in (glossary_data or []) if isinstance(item, dict)]
        self.is_glossary_dirty = False

        if hasattr(self, "glossary_widget"):
            self.glossary_widget.mark_current_state_as_saved()

        self._refresh_dirty_window_title()

    def _on_glossary_changed(self):
        if self.is_session_active or not self.output_folder:
            return
        current_glossary = self.glossary_widget.get_glossary()
        self.is_glossary_dirty = current_glossary != self.initial_glossary_state
        self._refresh_dirty_window_title()

    def _mark_settings_as_dirty(self):
        """Слот, который устанавливает флаг 'грязного' состояния и обновляет заголовок окна."""
        if self.is_settings_dirty or self.is_session_active:
            return
        if not self.local_set:
            return
        self.is_settings_dirty = True
        self._refresh_dirty_window_title()

    def _mark_promt_as_dirty(self):
        """Слот, который устанавливает флаг 'грязного' состояния и обновляет заголовок окна."""
        if self.is_settings_dirty or self.is_session_active:
            return
        self.is_settings_dirty = True
        self._refresh_dirty_window_title()


    def _get_ui_state_for_saving(self):
        """Собирает все релевантные настройки из UI в один словарь для сохранения."""
        state = {}
        state.update(self.model_settings_widget.get_settings())
        state.update(self.translation_options_widget.get_settings())
        state.update({
            'provider': self.key_management_widget.get_selected_provider(),
            'num_instances': self.instances_spin.value(),
            'custom_prompt': self.preset_widget.get_prompt(),
            'last_prompt_preset': self.preset_widget.get_current_preset_name(),
            'auto_translation': self.auto_translate_widget.get_settings(),
            THEME_SETTINGS_KEY: editable_theme_colors(getattr(self, '_ui_theme_colors', None)),
        })
        # Добавьте сюда другие настройки, если они должны сохраняться
        return state

    def _collect_global_ui_settings_for_restore(self):
        """Собирает глобальные настройки UI с учетом старого и нового форматов."""
        merged = {}

        raw_settings = self.settings_manager.load_settings()
        if isinstance(raw_settings, dict):
            merged.update(raw_settings)

        legacy_last_settings = self.settings_manager.get_last_settings()
        if isinstance(legacy_last_settings, dict):
            for key in (
                'model',
                'temperature',
                'temperature_override_enabled',
                'rpm_limit',
                'chunking',
                'dynamic_glossary',
                'thinking_enabled',
                'thinking_budget',
                'use_json_epub_pipeline',
            ):
                value = legacy_last_settings.get(key)
                if value is not None and (key not in merged or merged.get(key) is None):
                    merged[key] = value

        full_session_settings = self.settings_manager.load_full_session_settings()
        if isinstance(full_session_settings, dict):
            merged.update(full_session_settings)

        if THEME_SETTINGS_KEY not in merged:
            merged[THEME_SETTINGS_KEY] = editable_theme_colors()

        return merged

    def _restore_global_ui_settings(self):
        """Применяет сохраненные глобальные настройки после построения UI."""
        settings = self._collect_global_ui_settings_for_restore()
        if settings:
            self._apply_full_ui_settings(settings)

    def _save_global_ui_settings(self, clear_dirty=True):
        """Сохраняет полный набор глобальных настроек для следующего запуска."""
        self.settings_manager.save_ui_state(self._get_ui_state_for_saving())
        self.settings_manager.save_full_session_settings(self._get_full_ui_settings())

        if clear_dirty:
            self.is_settings_dirty = False
            self._refresh_dirty_window_title()

        print(f"[SETTINGS] Глобальные настройки сохранены в: {self.settings_manager.config_file}")

    def _save_current_ui_settings(self):
        """Сохраняет текущее состояние UI в активный файл настроек."""
        self._save_global_ui_settings()


    @QtCore.pyqtSlot()
    def _continue_loading_project_and_update_all(self):
        """
        Запускает полную асинхронную цепочку загрузки проекта.
        Используется после создания нового проекта или принудительной перезагрузки.
        """
        # Этот метод теперь просто "пробрасывает" вызов дальше,
        # обеспечивая единую точку входа для разных сценариев.
        self._process_selected_file()

    def _ask_and_filter_chapters(self):
        """
        Показывает диалог с опциями фильтрации для уже существующего списка глав.
        """
        if not self.project_manager or not self.html_files:
            return

        has_translated_chapters = any(self.project_manager.get_versions_for_original(ch) for ch in self.html_files)
        if not has_translated_chapters:
            return # Если переведенных глав нет, фильтровать нечего

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обновление списка глав")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText("Проект уже содержит переведенные главы. Что делать с текущим списком?")

        btn_skip_all = msg_box.addButton("Пропустить все переведенные", QMessageBox.ButtonRole.ActionRole)
        btn_skip_validated = msg_box.addButton("Пропустить только 'готовые'", QMessageBox.ButtonRole.ActionRole)
        btn_keep_all = msg_box.addButton("Оставить все как есть", QMessageBox.ButtonRole.AcceptRole)

        msg_box.exec()
        clicked_button = msg_box.clickedButton()

        if clicked_button == btn_skip_all:
            self._filter_all_translated_chapters(silent=True)
        elif clicked_button == btn_skip_validated:
            self._filter_validated_chapters(silent=True)
        # Если нажата "Оставить все", ничего не делаем


    def _handle_project_initialization(self, select_mode=True):
        """
        Главный оркестратор. Вызывается, когда и файл, и папка, и главы заданы.
        Версия 2.0: Корректно обрабатывает создание подпапки и перемещает оригинал.
        """
        import shutil

        file_path = self.selected_file
        folder_path = self.output_folder
        pending_cleanup_offer = self._pending_old_project_cleanup_offer
        self._pending_old_project_cleanup_offer = False

        history = self.settings_manager.load_project_history()

        def same_path(left, right):
            if not left or not right:
                return False
            return os.path.normcase(os.path.abspath(os.path.normpath(left))) == os.path.normcase(
                os.path.abspath(os.path.normpath(right))
            )

        is_known_project = any(
            same_path(p.get('epub_path'), file_path) and
            same_path(p.get('output_folder'), folder_path)
            for p in history
        )
        is_folder_reused = False

        # Изначально считаем, что будем работать с выбранными путями
        effective_folder = folder_path.replace('\\', '/')
        effective_file_path = file_path.replace('\\', '/')

        if not is_known_project:
            is_folder_reused = any(
                same_path(p.get('output_folder'), folder_path) and
                not same_path(p.get('epub_path'), file_path)
                for p in history
            )
            main_text = f"Вы выбрали папку <b>'{os.path.basename(folder_path)}'</b> для нового проекта."
            if is_folder_reused:
                main_text += "<br><br><b style='color: orange;'>Внимание:</b> Эта папка уже используется для другого проекта. Настоятельно рекомендуется создать подпапку."
            base_name = os.path.splitext(os.path.basename(file_path))[0]

            dialog = ProjectFolderDialog(self, main_text, base_name)
            if not dialog.exec():
                self.output_folder = None
                self.paths_widget.set_folder_path(None)
                self._on_project_data_changed()
                return

            choice = dialog.choice
            copy_original = dialog.copy_file_checked # Теперь это флаг "переместить"

            if choice == 'subfolder':
                subfolder_path = os.path.join(folder_path, base_name)
                try:
                    os.makedirs(subfolder_path, exist_ok=True)
                    # Переназначаем effective_folder на новую подпапку
                    effective_folder = subfolder_path
                except OSError as e:
                    QMessageBox.critical(self, "Ошибка", f"Не удалось создать подпапку:\n{e}")
                    return

            if copy_original: # Теперь это "переместить"
                try:
                    # os.path.join сам все нормализует
                    destination_path = os.path.join(effective_folder, os.path.basename(file_path))

                    if os.path.abspath(file_path) != os.path.abspath(destination_path):
                        shutil.move(file_path, destination_path)
                        # Обновляем путь к файлу, с которым будет работать сессия
                        effective_file_path = destination_path
                        print(f"[INFO] Оригинальный файл перемещен в папку проекта: {destination_path}")
                except (shutil.Error, OSError) as e:
                    QMessageBox.critical(self, "Ошибка перемещения", f"Не удалось переместить исходный файл:\n{e}")
                    return

        if pending_cleanup_offer or not is_known_project:
            self._maybe_offer_old_project_chapter_cleanup(effective_folder, effective_file_path)

        # Добавляем в историю уже финальные, эффективные пути
        self.settings_manager.add_to_project_history(effective_file_path, effective_folder)

        # Финально устанавливаем правильные пути в состояние диалога и UI
        self.selected_file = effective_file_path
        self.output_folder = effective_folder
        self.project_manager = TranslationProjectManager(self.output_folder)
        self.paths_widget.set_file_path(self.selected_file)
        self.paths_widget.set_folder_path(self.output_folder)

        if self.html_files:
            self._ask_and_filter_chapters()

        self._on_project_data_changed()

    def _update_cjk_options_for_widgets(self):
        """
        Анализирует данные, уже собранные виджетом оптимизации,
        и обновляет CJK опции.
        """
        if not self.html_files:
            self.model_settings_widget.update_cjk_options_availability(enabled=False)
            return

        # Берем готовые данные из виджета
        compositions = self.translation_options_widget.chapter_compositions
        if not compositions:
            self.model_settings_widget.update_cjk_options_availability(enabled=True, error=True)
            return

        is_any_cjk = any(comp.get('is_cjk', False) for comp in compositions.values())

        self.model_settings_widget.update_cjk_options_availability(enabled=True, is_cjk_recommended=is_any_cjk)

    @pyqtSlot(str)
    def on_file_selected(self, file_path):
        """Слот с логикой "разрыва связи" при смене файла."""
        if not file_path: return

        previous_file = self.selected_file
        file_changed = (
            not previous_file or
            os.path.abspath(previous_file) != os.path.abspath(file_path)
        )
        switching_to_new_source = (
            bool(self.output_folder and previous_file) and
            os.path.abspath(previous_file) != os.path.abspath(file_path)
        )

        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ: Атомарный сброс состояния ---
        # Если выбранный файл отличается от текущего, это означает смену контекста.
        # Мы ОБЯЗАНЫ немедленно сбросить список глав, чтобы предотвратить
        # использование списка глав от старого файла с новым файлом.
        if file_changed:
            self._pending_old_project_cleanup_offer = switching_to_new_source
            self.html_files = []
            # Немедленно обновляем UI, чтобы пользователь видел, что выбор глав сброшен
            self.paths_widget.update_chapters_info(0)
            if self.task_manager:
                # Очищаем очередь задач, так как она тоже относится к старому файлу
                self.task_manager.clear_all_queues()
            if switching_to_new_source:
                # При сознательной смене EPUB в уже выбранной папке не нужно
                # проверять новый файл как "старый" проект: дальше отработает
                # обычная инициализация проекта с переносом файла в папку проекта.
                self.project_manager = None
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---

        # Далее идет существующая логика проверки на "разрыв связи" с проектом.
        # Она остается без изменений, так как важна.
        if self.selected_file and self.output_folder and not switching_to_new_source:
            temp_pm = TranslationProjectManager(self.output_folder)
            cache_data = temp_pm.load_size_cache()

            if cache_data:
                _, is_cache_valid = get_epub_chapter_sizes_with_cache(temp_pm, file_path, return_cache_status=True)

                if not is_cache_valid:
                    QMessageBox.information(self, "Связь с проектом разорвана",
                                            f"Выбранный файл '{os.path.basename(file_path)}' не соответствует проекту в папке '{os.path.basename(self.output_folder)}'.\n\n"
                                            "Выбор папки был сброшен. Пожалуйста, выберите новую папку для этого файла.")
                    # --- РАДИКАЛЬНАЯ ОЧИСТКА ---
                    self.output_folder = None
                    self.project_manager = None
                    self.paths_widget.set_folder_path(None)
                    self.html_files = []
                    self.paths_widget.update_chapters_info(0) # Обновляем UI счетчика
                    if self.task_manager:
                        self.task_manager.clear_all_queues()
                    # --- КОНЕЦ ОЧИСТКИ ---

        # Устанавливаем новый выбранный файл
        self.selected_file = file_path
        self.paths_widget.set_file_path(file_path)

        # Запускаем дальнейшую обработку
        if file_changed or not self.html_files:
            self._process_selected_file()
        elif self.output_folder:
            self._handle_project_initialization()
        else:
            self._process_selected_file()
        self.check_ready()

    def on_folder_selected(self, folder):
        """Слот с логикой "разрыва связи" при смене папки."""
        if not folder: return

        if self.selected_file and self.output_folder:
            temp_pm = TranslationProjectManager(folder)
            cache_data = temp_pm.load_size_cache()

            if cache_data:
                _, is_cache_valid = get_epub_chapter_sizes_with_cache(temp_pm, self.selected_file, return_cache_status=True)

                if not is_cache_valid:
                    QMessageBox.information(self, "Связь с проектом разорвана",
                                            f"Папка '{os.path.basename(folder)}' содержит проект для другого файла.\n\n"
                                            "Выбор файла был сброшен. Пожалуйста, выберите EPUB, соответствующий этому проекту, или создайте новый проект в другой папке.")
                    # --- РАДИКАЛЬНАЯ ОЧИСТКА ---
                    self.selected_file = None
                    self.project_manager = None
                    self.html_files = []
                    self.paths_widget.set_file_path(None)
                    self.paths_widget.update_chapters_info(0)
                    if self.task_manager:
                        self.task_manager.clear_all_queues()
                    # --- КОНЕЦ ОЧИСТКИ ---

        self.output_folder = folder
        self.paths_widget.set_folder_path(folder)

        if self.selected_file:
            self._handle_project_initialization()
        else:
            self._on_project_data_changed()
        self.check_ready()

    def _on_swap_file_requested(self):
        """
        Процедура бесшовного переезда на новый файл EPUB.
        Переименовывает старый в _old_i, перемещает новый в папку проекта.
        """
        if not self.selected_file or not self.output_folder:
            return

        # 1. Выбор нового файла
        from ...utils.document_importer import DOCUMENT_INPUT_FILTER, convert_source_to_epub_with_dialog

        new_file_source, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите НОВУЮ версию исходника",
            os.path.dirname(self.selected_file),
            DOCUMENT_INPUT_FILTER,
        )
        if not new_file_source:
            return

        new_file_source = convert_source_to_epub_with_dialog(
            new_file_source,
            self.output_folder or os.path.dirname(self.selected_file),
            self,
        )
        if not new_file_source or os.path.abspath(new_file_source) == os.path.abspath(self.selected_file):
            return

        # 2. Анализ совместимости
        self.status_bar.set_permanent_message("Анализ совместимости глав...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)

        from ...utils.epub_tools import compare_epubs_for_swap, get_epub_chapter_order
        comparison_results = compare_epubs_for_swap(self.selected_file, new_file_source)

        QtWidgets.QApplication.restoreOverrideCursor()
        self.status_bar.clear_message()

        if comparison_results is None:
            QMessageBox.critical(self, "Ошибка", "Не удалось прочитать или сравнить файлы.")
            return

        # Сводка
        matches = [p for p, s in comparison_results.items() if s == 'match']
        mismatches = [p for p, s in comparison_results.items() if s == 'mismatch']
        new_chaps = [p for p, s in comparison_results.items() if s == 'new']

        msg = QMessageBox(self)
        msg.setWindowTitle("Переезд на новую версию файла")
        msg.setIcon(QMessageBox.Icon.Question)
        msg_text = (
            f"✅ <b>Совпало: {len(matches)}</b> (переводы сохранятся)\n"
            f"❌ <b>Изменилось: {len(mismatches)}</b> (переводы будут удалены)\n"
            f"🆕 <b>Новых глав: {len(new_chaps)}</b>"
        )
        msg.setText(msg_text)
        msg.setInformativeText(
            "Программа переименует текущий файл в '_old', перенесет новый файл на его место "
            "и обновит базу проекта. Продолжить?"
        )
        btn_proceed = msg.addButton("Да, выполнить переезд", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() != btn_proceed:
            return

        # 3. ФИЗИЧЕСКИЙ ПЕРЕЕЗД ФАЙЛОВ
        import shutil
        try:
            # А. Генерируем имя для архивации старого
            base, ext = os.path.splitext(self.selected_file)
            i = 1
            while os.path.exists(f"{base}_old_{i}{ext}"):
                i += 1
            old_version_path = f"{base}_old_{i}{ext}"

            # Б. Архивируем старый (переименовываем)
            os.rename(self.selected_file, old_version_path)

            # В. Перемещаем новый файл на место старого (или рядом, если имена разные)
            # Мы будем использовать путь в папке проекта для нового файла
            target_new_path = os.path.join(os.path.dirname(self.selected_file), os.path.basename(new_file_source))

            # Если пользователь выбрал файл, который и так лежит в этой папке (но под другим именем)
            if os.path.abspath(new_file_source) != os.path.abspath(target_new_path):
                shutil.copy2(new_file_source, target_new_path)

            # Запоминаем новый путь
            new_active_file = target_new_path

        except Exception as e:
            QMessageBox.critical(self, "Ошибка файловой системы", f"Не удалось переместить файлы: {e}")
            return

        # 4. ЧИСТКА КАРТЫ ПРОЕКТА И ДИСКА
        self.project_manager.reload_data_from_disk()
        files_deleted_count = 0

        # Удаляем переводы для несовпавших глав
        for path in mismatches:
            versions = self.project_manager.get_versions_for_original(path)
            for suffix, rel_path in versions.items():
                full_path = os.path.join(self.output_folder, rel_path)
                if os.path.exists(full_path):
                    try: os.remove(full_path); files_deleted_count += 1
                    except: pass

            # Сносим ветку из JSON
            with self.project_manager.lock:
                current_data = self.project_manager._load_unsafe()
                if path in current_data: del current_data[path]
                self.project_manager._save_unsafe(current_data)

        # Удаляем из карты главы, которых вообще нет в новом EPUB
        current_map = self.project_manager.get_full_map()
        new_file_all_paths = set(comparison_results.keys())
        for old_path in list(current_map.keys()):
            if old_path not in new_file_all_paths:
                versions = current_map[old_path]
                for suffix, rel_path in versions.items():
                    full_path = os.path.join(self.output_folder, rel_path)
                    if os.path.exists(full_path):
                        try: os.remove(full_path); files_deleted_count += 1
                        except: pass
                with self.project_manager.lock:
                    data = self.project_manager._load_unsafe()
                    if old_path in data: del data[old_path]
                    self.project_manager._save_unsafe(data)

        # 5. ОБНОВЛЕНИЕ UI
        self.selected_file = new_active_file
        self.paths_widget.set_file_path(self.selected_file)

        # Обновляем историю проектов
        self.settings_manager.add_to_project_history(self.selected_file, self.output_folder)

        # Берем все главы из нового файла как текущий выбор
        self.html_files = get_epub_chapter_order(self.selected_file)

        # Полная перерисовка
        self._on_project_data_changed(offer_snapshot_restore=False)

        QMessageBox.information(self, "Переезд завершен",
            f"Новый файл: {os.path.basename(new_active_file)}\n"
            f"Старая версия сохранена как: {os.path.basename(old_version_path)}\n\n"
            f"Удалено неактуальных переводов: {files_deleted_count}.")


    def _on_folder_sync_finished(self, is_project_ready, message):
        """
        Слот, который вызывается после завершения фоновой синхронизации папки.
        Версия 2.0: Использует новые, централизованные методы для фильтрации и обновления.
        """
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            self.output_folder = None
            self.project_manager = None
            self.paths_widget.set_folder_path(None)
            self.check_ready()
            return

        # 1. Загружаем ассеты проекта (например, глоссарий).
        self._process_project_folder(self.output_folder)

        # 2. Вызываем "умный" диалог, который предложит отфильтровать список глав, если это необходимо.
        self._ask_and_filter_chapters()

        # 3. Вызываем единый "оркестратор" для обновления всего UI на основе
        #    (возможно, измененного) списка глав.
        self._on_project_data_changed()

    def _handle_backup_restore(self):
        """
        Обрабатывает нажатие на кнопку 'Очередь...'.
        Предлагает сохранить или загрузить состояние очереди.
        """
        if not self.output_folder or not self.selected_file:
            QtWidgets.QMessageBox.warning(self, "Проект не определен", "Для работы с бэкапом очереди необходимо выбрать файл и папку проекта.")
            return

        if not (self.engine and self.engine.task_manager):
            return

        snapshot_path = os.path.join(self.output_folder, "queue_snapshot.db")
        has_snapshot = os.path.exists(snapshot_path)

        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Управление очередью задач")
        msg_box.setText("Вы можете сохранить текущее состояние очереди на диск или загрузить ранее сохраненное.")

        if has_snapshot:
            # Получаем время изменения файла для инфо
            import datetime
            mtime = os.path.getmtime(snapshot_path)
            dt = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            msg_box.setInformativeText(f"На диске найден бэкап от: {dt}")
        else:
            msg_box.setInformativeText("Сохраненных бэкапов не найдено.")

        btn_save = msg_box.addButton("💾 Сохранить текущую", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        btn_load = msg_box.addButton("📂 Загрузить с диска", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        btn_cancel = msg_box.addButton("Отмена", QtWidgets.QMessageBox.ButtonRole.RejectRole)

        btn_load.setEnabled(has_snapshot)

        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked == btn_save:
            # СОХРАНЕНИЕ
            if self.engine.task_manager.save_queue_snapshot(snapshot_path, self.selected_file):
                self._write_snapshot_ui_settings(snapshot_path, self._get_full_ui_settings())
                QtWidgets.QMessageBox.information(self, "Успех", "Очередь задач успешно сохранена в файл проекта.")
            else:
                QtWidgets.QMessageBox.critical(self, "Ошибка", "Не удалось сохранить очередь.")

        elif clicked == btn_load:
            # ЗАГРУЗКА
            try:
                self._restore_queue_snapshot(snapshot_path, show_success=True)

            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Ошибка загрузки", f"Не удалось загрузить очередь:\n{e}")
                # Если загрузка провалилась (например, хеш не совпал), лучше очистить текущий UI от греха подальше,
                # или оставить как есть, если ошибка была перехвачена до деструктивных действий.
                # В load_queue_snapshot база восстанавливается атомарно, так что если исключение вылетело -
                # скорее всего база в памяти осталась старой (если ошибка до backup) или пустой.
                # Обновим UI на всякий случай.
                self._on_project_data_changed()

    def _get_snapshot_path(self):
        if not self.output_folder:
            return None
        return os.path.join(self.output_folder, "queue_snapshot.db")

    def _schedule_snapshot_save(self):
        if self._snapshot_restore_in_progress:
            return
        if not (self.is_session_active or (self.engine and self.engine.session_id)):
            return
        if not (self.selected_file and self.output_folder and self.engine and self.engine.task_manager):
            return
        self._snapshot_save_requested = True
        if self._snapshot_autosave_worker and self._snapshot_autosave_worker.isRunning():
            return
        self._snapshot_save_timer.start()

    def _on_snapshot_autosave_finished(self):
        snapshot_path = self._get_snapshot_path()
        if (
            snapshot_path
            and self._snapshot_autosave_worker
            and getattr(self._snapshot_autosave_worker, 'result', None)
        ):
            self._write_snapshot_ui_settings(snapshot_path, self._get_full_ui_settings())
        self._snapshot_autosave_worker = None
        if self._snapshot_save_requested and self.is_session_active:
            self._snapshot_save_timer.start()

    def _save_snapshot_async(self, force=False):
        snapshot_path = self._get_snapshot_path()
        if not snapshot_path or not self.selected_file:
            return
        if not (self.engine and self.engine.task_manager):
            return
        if not force and not self._snapshot_save_requested:
            return
        if self._snapshot_autosave_worker and self._snapshot_autosave_worker.isRunning():
            self._snapshot_save_requested = True
            return
        self._snapshot_save_requested = False

        self._snapshot_autosave_worker = TaskDBWorker(
            self.engine.task_manager.save_queue_snapshot,
            snapshot_path,
            self.selected_file,
            True
        )
        self._snapshot_autosave_worker.finished.connect(self._on_snapshot_autosave_finished)
        self._snapshot_autosave_worker.start()

    def _restore_queue_snapshot(self, snapshot_path: str, show_success: bool = False) -> bool:
        if not (self.engine and self.engine.task_manager):
            return False

        try:
            snapshot_settings = self._read_snapshot_ui_settings(snapshot_path)
            restored_chapters = self.engine.task_manager.load_queue_snapshot(snapshot_path, self.selected_file)
            if restored_chapters is None:
                return False

            self._snapshot_restore_in_progress = True
            if snapshot_settings:
                self._apply_full_ui_settings(snapshot_settings)
            self.html_files = restored_chapters
            self._on_project_data_changed(offer_snapshot_restore=False, rebuild_tasks=False)

            if show_success:
                QtWidgets.QMessageBox.information(
                    self,
                    "Успех",
                    f"Очередь восстановлена. Список глав обновлен ({len(self.html_files)} шт)."
                )
            return True
        finally:
            self._snapshot_restore_in_progress = False

    def _maybe_offer_snapshot_restore(self):
        if self._snapshot_restore_in_progress or self.is_session_active:
            return
        if not (self.selected_file and self.output_folder and self.engine and self.engine.task_manager):
            return

        snapshot_path = self._get_snapshot_path()
        if not snapshot_path or not os.path.exists(snapshot_path):
            return

        project_key = (self.selected_file, self.output_folder)
        if project_key in self._snapshot_prompted_projects:
            return

        meta = self.engine.task_manager.read_queue_snapshot_meta(snapshot_path)
        if not meta:
            return

        saved_task_count = meta.get('saved_task_count')
        if saved_task_count is None:
            saved_task_count = meta.get('recoverable_tasks', 0)
        if saved_task_count <= 0:
            return

        saved_at = meta.get('saved_at')
        saved_at_text = "неизвестно"
        if saved_at:
            saved_at_text = datetime.fromtimestamp(saved_at).strftime('%Y-%m-%d %H:%M:%S')

        pending = meta.get('count_pending', 0)
        in_progress = meta.get('count_in_progress', 0)
        failed = meta.get('count_failed', 0)
        completed = meta.get('count_completed', 0)
        held = meta.get('count_held', 0)

        self._snapshot_prompted_projects.add(project_key)

        restore = QtWidgets.QMessageBox.question(
            self,
            "Восстановить прошлый список задач?",
            (
                "Для этого проекта найден сохраненный снимок очереди и статусов.\n\n"
                f"Сохранен: {saved_at_text}\n"
                f"Сохранено задач: {saved_task_count}\n"
                f"Ожидают: {pending}, в работе: {in_progress}, готово: {completed}, "
                f"заморожены: {held}, с ошибкой: {failed}\n\n"
                "Восстановить список задач сейчас?"
            ),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes
        )
        if restore == QtWidgets.QMessageBox.StandardButton.Yes:
            try:
                self._restore_queue_snapshot(snapshot_path, show_success=True)
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Ошибка загрузки",
                    f"Не удалось восстановить очередь:\n{e}"
                )
                self._on_project_data_changed()

    def _get_preflight_task_payloads(self):
        payloads = []
        if self.engine and self.engine.task_manager:
            payloads = [payload for _, payload in self.engine.task_manager.get_all_pending_tasks()]
        if not payloads and self.selected_file and self.html_files:
            payloads = [('epub', self.selected_file, chapter_path) for chapter_path in self.html_files]
        return payloads

    def _build_preflight_report(self, settings: dict | None = None) -> str:
        settings = settings or self.get_settings()
        task_payloads = self._get_preflight_task_payloads()
        if not task_payloads:
            return "Очередь пуста. Добавьте главы или пересоберите задачи перед оценкой."

        token_counter = TokenCounter()
        model_config = settings.get('model_config') or {}
        model_name = settings.get('model') or model_config.get('id') or "Неизвестная модель"
        prompt_tokens = token_counter.estimate_tokens(settings.get('custom_prompt'))
        system_tokens = 0
        if settings.get('use_system_instruction') and settings.get('system_instruction'):
            system_tokens = token_counter.estimate_tokens(settings.get('system_instruction'))

        glossary_blob = json.dumps(settings.get('full_glossary_data') or {}, ensure_ascii=False)
        glossary_tokens = token_counter.estimate_tokens(glossary_blob)
        glossary_factor = 0.2 if settings.get('dynamic_glossary') else 1.0
        effective_glossary_tokens = int(round(glossary_tokens * glossary_factor))
        per_task_overhead = prompt_tokens + system_tokens + effective_glossary_tokens

        chapter_html_cache = {}
        chapter_text_cache = {}
        unique_chapters = set()
        archive_error = None

        try:
            with zipfile.ZipFile(self.selected_file, 'r') as zf:
                for payload in task_payloads:
                    task_type = payload[0]
                    if task_type == 'epub_chunk':
                        unique_chapters.add(payload[2])
                        continue
                    if task_type == 'epub_batch':
                        chapter_paths = payload[2]
                    else:
                        chapter_paths = [payload[2]]
                    for chapter_path in chapter_paths:
                        unique_chapters.add(chapter_path)
                        if chapter_path in chapter_html_cache:
                            continue
                        content = zf.read(chapter_path).decode('utf-8', 'ignore')
                        chapter_html_cache[chapter_path] = content
                        chapter_text_cache[chapter_path] = BeautifulSoup(content, 'html.parser').get_text()
        except Exception as e:
            archive_error = str(e)

        total_html_tokens = 0
        total_output_tokens = 0
        max_input_tokens = 0
        max_output_tokens = 0
        total_source_chars = 0
        task_type_counter = Counter()
        chapter_occurrences = Counter()
        cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
        cjk_divider = 1.5

        for payload in task_payloads:
            task_type = payload[0]
            task_type_counter[task_type] += 1
            task_input_tokens = 0
            task_output_tokens = 0
            task_source_chars = 0

            if task_type == 'epub_chunk':
                chapter_occurrences[payload[2]] += 1
                chunk_html = payload[3] or ""
                chunk_text = BeautifulSoup(chunk_html, 'html.parser').get_text()
                is_cjk = bool(cjk_pattern.search(chunk_text))
                potential_size, tags_size = calculate_potential_output_size(chunk_html, is_cjk)
                text_size = max(potential_size - tags_size, 0)
                task_input_tokens = token_counter.estimate_tokens(chunk_html)
                task_output_tokens = int(math.ceil(
                    (tags_size / api_config.CHARS_PER_ASCII_TOKEN) +
                    (text_size / (cjk_divider if is_cjk else api_config.CHARS_PER_CYRILLIC_TOKEN))
                ))
                task_source_chars = len(chunk_html)
            else:
                chapter_paths = payload[2] if task_type == 'epub_batch' else [payload[2]]
                for chapter_path in chapter_paths:
                    chapter_occurrences[chapter_path] += 1
                    chapter_html = chapter_html_cache.get(chapter_path, "")
                    chapter_text = chapter_text_cache.get(chapter_path, "")
                    is_cjk = bool(cjk_pattern.search(chapter_text))
                    potential_size, tags_size = calculate_potential_output_size(chapter_html, is_cjk)
                    text_size = max(potential_size - tags_size, 0)
                    task_input_tokens += token_counter.estimate_tokens(chapter_html)
                    task_output_tokens += int(math.ceil(
                        (tags_size / api_config.CHARS_PER_ASCII_TOKEN) +
                        (text_size / (cjk_divider if is_cjk else api_config.CHARS_PER_CYRILLIC_TOKEN))
                    ))
                    task_source_chars += len(chapter_html)

            task_total_input = task_input_tokens + per_task_overhead
            total_html_tokens += task_input_tokens
            total_output_tokens += task_output_tokens
            total_source_chars += task_source_chars
            max_input_tokens = max(max_input_tokens, task_total_input)
            max_output_tokens = max(max_output_tokens, task_output_tokens)

        total_input_tokens = total_html_tokens + (len(task_payloads) * per_task_overhead)
        workers = max(1, settings.get('num_instances') or 1)
        rpm_limit = settings.get('rpm_limit') or model_config.get('rpm') or 1
        concurrency_hint = settings.get('max_concurrent_requests') or workers
        effective_parallel = max(1, min(workers, concurrency_hint))
        time_by_rpm = (len(task_payloads) / max(1, rpm_limit)) * 60.0
        time_by_parallel = math.ceil(len(task_payloads) / effective_parallel) * 12.0
        rough_duration = max(time_by_rpm, time_by_parallel)
        rpm_floor = (len(task_payloads) / max(1, rpm_limit)) * 60.0

        cost_text = "н/д для этой модели"
        if 'gemini' in model_name.lower():
            estimated_cost = token_counter.estimate_cost(total_input_tokens, total_output_tokens, model_name)
            cost_text = f"${estimated_cost:.4f}"

        model_output_limit = model_config.get('max_output_tokens', api_config.default_max_output_tokens())
        safe_output_limit = int(model_output_limit * api_config.MODEL_OUTPUT_SAFETY_MARGIN)

        risk_notes = []
        if max_output_tokens > safe_output_limit:
            risk_notes.append("Высокий риск упора в лимит ответа модели на самой большой задаче.")
        elif max_output_tokens > safe_output_limit * 0.8:
            risk_notes.append("Самая крупная задача близка к безопасному лимиту ответа модели.")

        if rpm_limit < workers:
            risk_notes.append("RPM ниже числа воркеров: часть параллелизма будет простаивать из-за лимита запросов.")

        avg_html_tokens = total_html_tokens / max(1, len(task_payloads))
        if per_task_overhead > avg_html_tokens:
            risk_notes.append("Промпт и глоссарий тяжелее среднего HTML-входа, это заметно влияет на цену и время.")

        if settings.get('dynamic_glossary') and glossary_tokens:
            risk_notes.append("Глоссарий оценен с понижающим коэффициентом 20%, потому что включена динамическая фильтрация.")

        if archive_error:
            risk_notes.append(f"Не удалось полностью прочитать EPUB для точного прогноза: {archive_error}")

        duplicated_chapters = sum(1 for count in chapter_occurrences.values() if count > 1)
        report_lines = [
            "Сводка",
            f"Файл: {os.path.basename(self.selected_file)}",
            f"Модель: {model_name}",
            f"Очередь: {len(task_payloads)} запросов, {len(unique_chapters)} уникальных глав",
            (
                f"Типы задач: обычные {task_type_counter.get('epub', 0)}, "
                f"пакеты {task_type_counter.get('epub_batch', 0)}, "
                f"чанки {task_type_counter.get('epub_chunk', 0)}"
            ),
            "",
            "Нагрузка",
            f"Символов исходника: ~{total_source_chars:,}".replace(",", " "),
            f"Входные токены HTML: ~{int(round(total_html_tokens)):,}".replace(",", " "),
            f"Служебные токены на запрос: ~{per_task_overhead:,}".replace(",", " "),
            f"Общий вход: ~{int(round(total_input_tokens)):,} токенов".replace(",", " "),
            f"Общий выход: ~{int(round(total_output_tokens)):,} токенов".replace(",", " "),
            f"Макс. вход на задачу: ~{int(round(max_input_tokens)):,} токенов".replace(",", " "),
            f"Макс. выход на задачу: ~{int(round(max_output_tokens)):,} токенов".replace(",", " "),
            "",
            "Время и лимиты",
            f"Воркеров: {workers}, эффективный параллелизм: {effective_parallel}",
            f"RPM лимит: {rpm_limit}",
            f"Технический минимум по RPM: {_format_duration(rpm_floor)}",
            f"Грубая оценка времени с учетом параллелизма: {_format_duration(rough_duration)}",
            f"Безопасный лимит ответа модели: ~{safe_output_limit:,} токенов".replace(",", " "),
            "",
            "Стоимость",
            f"Оценка стоимости: {cost_text}",
            f"Промпт: ~{prompt_tokens:,} токенов".replace(",", " "),
            f"System instruction: ~{system_tokens:,} токенов".replace(",", " "),
            (
                f"Глоссарий: ~{glossary_tokens:,} токенов "
                f"(в расчете используется ~{effective_glossary_tokens:,})"
            ).replace(",", " "),
            "",
            "Наблюдения",
            f"Повторно встречающихся глав в очереди: {duplicated_chapters}",
        ]

        if risk_notes:
            report_lines.append("Риски и заметки:")
            report_lines.extend(f"- {note}" for note in risk_notes)
        else:
            report_lines.append("Риски и заметки:")
            report_lines.append("- Критичных признаков перегруза по текущей конфигурации не найдено.")

        return "\n".join(report_lines)

    def _emit_task_manipulation_signal(self, action: str, payload):
        """
        Общий метод для ЗАПУСКА фоновых команд в TaskManager и обновления UI.
        Версия 2.0: Использует QThread для предотвращения зависания UI.
        """
        if not (self.engine and self.engine.task_manager):
            return

        target_method = None
        args = []

        if action in ['top', 'bottom', 'up', 'down']:
            target_method = self.engine.task_manager.reorder_tasks
            args = [action, payload]
        elif action == 'remove':
            target_method = self.engine.task_manager.remove_tasks
            args = [payload]
        elif action == 'duplicate':
            target_method = self.engine.task_manager.duplicate_tasks
            args = [payload]
        elif action == 'split_batch':
            target_method = self.engine.task_manager.split_batches_into_chapters
            args = [payload]
        elif action == 'reorder_batch_chapters':
            target_method = self.engine.task_manager.reorder_batch_chapters
            args = [payload[0], payload[1]]

        if not target_method:
            return

        # --- НОВАЯ ЛОГИКА С QTHREAD ---
        # 1. Блокируем UI, чтобы пользователь не нажал ничего лишнего
        self.task_management_widget.setEnabled(False)
        status_message = "Обновление списка задач..."
        if action == 'split_batch':
            status_message = "Разбиваю пакеты на главы..."
        elif action == 'reorder_batch_chapters':
            status_message = "Сохраняю порядок глав в пакете..."
        self.status_bar.set_permanent_message(status_message)

        # 2. Создаем и запускаем "грузчика"
        self.db_worker = TaskDBWorker(target_method, *args)

        # 3. После того как грузчик закончит, разблокируем UI
        self.db_worker.finished.connect(self._on_db_worker_finished)
        self.db_worker.start()

    def _on_db_worker_finished(self):
        """Слот, который вызывается по завершении фоновой DB-задачи."""
        self.status_bar.clear_message()
        self.task_management_widget.setEnabled(True)


    def _handle_task_reorder(self, action: str, task_ids: list):
        self._emit_task_manipulation_signal(action, task_ids)

    def _handle_task_duplication(self, task_ids: list):
        self._emit_task_manipulation_signal('duplicate', task_ids)

    def _handle_task_removal(self, task_ids: list):
        self._emit_task_manipulation_signal('remove', task_ids)

    def _handle_batch_split(self, task_ids: list):
        self._emit_task_manipulation_signal('split_batch', task_ids)

    def _handle_batch_chapter_reorder(self, task_id, chapter_order):
        self._emit_task_manipulation_signal('reorder_batch_chapters', (task_id, chapter_order))

    def _resolve_translated_preview_path(self, chapter_path: str):
        if not chapter_path or not self.project_manager:
            return None, None

        try:
            self.project_manager.reload_data_from_disk()
        except Exception:
            pass

        versions = self.project_manager.get_versions_for_original(chapter_path) or {}
        candidates = []
        for suffix, rel_path in versions.items():
            if suffix == 'filtered' or not rel_path:
                continue

            full_path = os.path.join(self.project_manager.project_folder, rel_path.replace('/', os.sep))
            if not os.path.exists(full_path):
                continue

            try:
                modified_at = os.path.getmtime(full_path)
            except OSError:
                modified_at = 0

            # Если версий несколько, показываем самый недавно измененный файл.
            # При равном времени предпочитаем готовую версию.
            priority = 0 if suffix == '_validated.html' else 1
            candidates.append((-modified_at, priority, full_path, suffix))

        if not candidates:
            return None, None

        candidates.sort()
        _, _, preview_path, preview_suffix = candidates[0]
        return preview_path, preview_suffix

    def _open_project_chapter_preview(self, chapter_path: str, preview_path: str, preview_suffix: str):
        with open(preview_path, 'r', encoding='utf-8', errors='ignore') as f:
            preview_content = f.read()

        suffix_label = "готовая версия" if preview_suffix == '_validated.html' else f"версия {preview_suffix}"
        dialog = ChapterTextPreviewDialog(
            title=f"Предпросмотр результата: {os.path.basename(preview_path)}",
            chapter_path=preview_path,
            text_content=preview_content,
            parent=self,
            render_html=True,
            path_caption=(
                f"Источник: итоговый файл проекта ({suffix_label})\n"
                f"{preview_path}\n\n"
                f"Глава EPUB:\n{chapter_path}"
            ),
        )
        dialog.exec()

    def _open_chapter_preview_from_queue(self, epub_path: str, chapter_path: str):
        if not chapter_path:
            QMessageBox.information(self, "Предпросмотр", "Не удалось определить главу для предпросмотра.")
            return

        translated_preview_path, translated_preview_suffix = self._resolve_translated_preview_path(chapter_path)
        if translated_preview_path:
            try:
                self._open_project_chapter_preview(
                    chapter_path,
                    translated_preview_path,
                    translated_preview_suffix or "",
                )
                return
            except Exception as e:
                print(f"[WARN] Не удалось открыть итоговый файл для предпросмотра {translated_preview_path}: {e}")

        html_content = None
        last_error = None

        candidate_epubs = []
        if epub_path:
            candidate_epubs.append(epub_path)
        if self.selected_file and self.selected_file not in candidate_epubs:
            candidate_epubs.append(self.selected_file)

        for source_epub in candidate_epubs:
            try:
                with zipfile.ZipFile(open(source_epub, 'rb'), 'r') as zf:
                    html_content = zf.read(chapter_path).decode('utf-8', 'ignore')
                break
            except Exception as e:
                last_error = e

        if html_content is None:
            QMessageBox.warning(
                self,
                "Предпросмотр",
                f"Не удалось открыть текст главы:\n{chapter_path}\n\n{last_error}"
            )
            return

        preview_text = html_content

        dialog = ChapterTextPreviewDialog(
            title=f"Предпросмотр исходника: {os.path.basename(chapter_path)}",
            chapter_path=chapter_path,
            text_content=preview_text,
            parent=self,
            path_caption=f"Источник: глава из EPUB\n{chapter_path}",
        )
        dialog.exec()

    def _filter_validated_chapters(self, silent=False):
        """
        Фильтрует self.html_files, оставляя только те главы, для которых НЕТ 'готовой' версии.
        """
        if not self.project_manager or not self.html_files:
            return

        chapters_to_keep = [ch for ch in self.html_files if '_validated.html' not in self.project_manager.get_versions_for_original(ch)]

        if len(chapters_to_keep) < len(self.html_files):
            self.html_files = chapters_to_keep
            if not silent:
                QMessageBox.information(self, "Главы отфильтрованы", f"Скрыты 'готовые' главы. Осталось для перевода: {len(self.html_files)}.")
                # Обновляем UI, так как это был прямой вызов от пользователя
                self._on_project_data_changed()
        elif not silent:
            QMessageBox.information(self, "Нет изменений", "В текущем списке нет глав, помеченных как 'готовые'.")

    def _filter_all_translated_tasks(self):
        """Фильтрует задачи, убирая все, у которых есть любая версия перевода."""
        all_possible_suffixes = api_config.all_translated_suffixes() + ['_validated.html']

        def filter_logic(chapters_to_filter):
            untracked = []
            chapters_to_keep = []
            for chapter_path in chapters_to_filter:
                base_name = os.path.splitext(os.path.basename(chapter_path))[0]
                internal_dir = os.path.dirname(chapter_path)

                is_translated = False
                for suffix in all_possible_suffixes:
                    full_disk_path = os.path.join(self.project_manager.project_folder, internal_dir, f"{base_name}{suffix}")
                    if os.path.exists(full_disk_path):
                        is_translated = True
                        # Проверяем, зарегистрирован ли файл, и добавляем в список, если нет
                        versions = self.project_manager.get_versions_for_original(chapter_path)
                        if suffix not in versions:
                            relative_path = os.path.relpath(full_disk_path, self.project_manager.project_folder)
                            untracked.append((chapter_path, suffix, relative_path))
                        break # Нашли перевод, дальше не ищем

                if not is_translated:
                    chapters_to_keep.append(chapter_path)

            return chapters_to_keep, untracked

        filtered_chapters, original_count = self._flatten_and_filter_tasks(filter_logic)

        if filtered_chapters is None: # Если была ошибка
            return

        if len(filtered_chapters) == original_count:
            QMessageBox.information(self, "Нет изменений", "Не найдено переведенных глав для скрытия.")
        else:
            QMessageBox.information(self, "Готово", "Список задач отфильтрован и пересобран.")

    def _flatten_and_filter_tasks(self, filter_function):
        """
        Универсальный оркестратор фильтрации.
        1. "Расплющивает" все задачи в упорядоченный список глав.
        2. Применяет переданную функцию-фильтр.
        3. Запускает полную пересборку задач на основе отфильтрованного списка.
        """
        if not (self.project_manager and self.engine and self.engine.task_manager):
            QMessageBox.information(self, "Нет данных", "Менеджер проекта или задач не инициализирован.")
            return None, 0 # Возвращаем None, чтобы показать, что операция не удалась

        tasks_to_check = self.engine.task_manager.get_all_tasks_for_rebuild()
        if not tasks_to_check:
            QMessageBox.information(self, "Нет данных", "Список задач для фильтрации пуст.")
            return None, 0

        # Шаг 1: "Расплющивание"
        ordered_unique_chapters = []
        seen_chapters = set()
        for task_id, task_payload in tasks_to_check:
            chapters_in_task = []
            task_type = task_payload[0]
            if task_type in ('epub', 'epub_chunk'):
                chapters_in_task.append(task_payload[2])
            elif task_type == 'epub_batch':
                chapters_in_task.extend(task_payload[2])

            for chapter in chapters_in_task:
                if chapter not in seen_chapters:
                    ordered_unique_chapters.append(chapter)
                    seen_chapters.add(chapter)

        original_chapter_count = len(ordered_unique_chapters)

        # Шаг 2: Фильтрация
        self.project_manager.reload_data_from_disk()

        # Функция filter_function вернет отфильтрованный список глав и список "беспризорников"
        filtered_chapters, untracked_files = filter_function(ordered_unique_chapters)
        if untracked_files:
            self.project_manager.register_multiple_translations(untracked_files)
            print(f"[INFO] Фильтр обнаружил и зарегистрировал {len(untracked_files)} ранее неучтенных файлов.")

        # Шаг 3: Пересборка
        # Обновляем self.html_files - это наш новый источник правды для UI
        self.html_files = filtered_chapters

        # Запускаем единый "оркестратор" для полного и консистентного
        # обновления всего UI на основе нового списка глав.
        self._on_project_data_changed(offer_snapshot_restore=False)

        # Возвращаем результат для отображения сообщения пользователю.
        return filtered_chapters, original_chapter_count

    def _maybe_offer_old_project_chapter_cleanup(self, folder_path, file_path):
        text_folder = os.path.join(folder_path, "OEBPS", "Text")
        if not os.path.isdir(text_folder):
            return False

        existing_files = []
        for root, _, files in os.walk(text_folder):
            for filename in files:
                existing_files.append(os.path.join(root, filename))

        if not existing_files:
            return False

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Старые главы в проекте")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText(
            f"В папке проекта уже найдено {len(existing_files)} файл(ов) в 'OEBPS\\Text'."
        )
        msg_box.setInformativeText(
            f"Вы добавляете новый EPUB '{os.path.basename(file_path)}' в существующий проект.\n\n"
            "Можно удалить прошлые главы из 'OEBPS\\Text' и очистить связанные записи "
            "в 'translation_map.json', чтобы старый текст не смешивался с новым.\n\n"
            "Удалить прошлые главы?"
        )
        remove_button = msg_box.addButton("Да, удалить прошлые главы", QMessageBox.ButtonRole.YesRole)
        keep_button = msg_box.addButton("Нет, оставить", QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(keep_button)
        msg_box.exec()

        if msg_box.clickedButton() != remove_button:
            return False

        cleanup_result = TranslationProjectManager(folder_path).cleanup_translations_in_subtree("OEBPS/Text")
        if cleanup_result["failed"]:
            details = "\n".join(
                f"- {path}" for path, _ in cleanup_result["failed"][:5]
            )
            if len(cleanup_result["failed"]) > 5:
                details += f"\n… и еще {len(cleanup_result['failed']) - 5}."
            QMessageBox.warning(
                self,
                "Очистка выполнена не полностью",
                "Не все прошлые главы удалось удалить.\n\n"
                f"Удалено файлов: {cleanup_result['removed_files']}\n"
                f"Очищено записей карты: {cleanup_result['removed_entries']}\n\n"
                f"Проблемные пути:\n{details}"
            )

        return True

    def _filter_validated_tasks(self):
        """Фильтрует задачи, убирая 'готовые'."""
        VALIDATED_SUFFIX = "_validated.html"

        def filter_logic(chapters_to_filter):
            # Мы можем просто переиспользовать существующий _is_chapter_validated!
            untracked = []
            chapters_to_keep = [
                ch for ch in chapters_to_filter
                if not self._is_chapter_validated(ch, VALIDATED_SUFFIX, untracked)
            ]
            return chapters_to_keep, untracked

        filtered_chapters, original_count = self._flatten_and_filter_tasks(filter_logic)

        if filtered_chapters is None:
            return

        if len(filtered_chapters) == original_count:
            QMessageBox.information(self, "Нет изменений", "Не найдено 'готовых' глав для скрытия.")
        else:
            QMessageBox.information(self, "Готово", "Список задач отфильтрован и пересобран. 'Готовые' главы скрыты.")


    def _is_chapter_validated(self, chapter_path, validated_suffix, untracked_list):
        """
        Вспомогательный метод. Проверяет, существует ли для главы "готовый" файл.
        Если да, то также проверяет, зарегистрирован ли он, и при необходимости добавляет в список для тихого обновления.
        Возвращает True, если глава считается "готовой", иначе False.
        """
        base_name = os.path.splitext(os.path.basename(chapter_path))[0]
        internal_dir = os.path.dirname(chapter_path)
        validated_filename = f"{base_name}{validated_suffix}"
        full_disk_path = os.path.join(self.project_manager.project_folder, internal_dir, validated_filename)

        if os.path.exists(full_disk_path):
            # Файл существует. Проверяем, есть ли он в карте.
            versions = self.project_manager.get_versions_for_original(chapter_path)
            if validated_suffix not in versions:
                relative_path = os.path.relpath(full_disk_path, self.project_manager.project_folder)
                untracked_list.append((chapter_path, validated_suffix, relative_path))
            return True # Глава "готова"

        return False # Файл не найден, глава не "готова"

    def _ask_and_run_migration(self, migrator, file_count):
        """Показывает диалог с предложением о миграции и запускает ее."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Обнаружен старый проект")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText(f"В выбранной папке найдено {file_count} файлов в старом 'плоском' формате.")

        msg_box.setInformativeText(
            "Программа может попытаться автоматически преобразовать этот проект в новую структурированную систему "
            "(с вложенными папками и файлом-картой 'translation_map.json').\n\n"
            "Это позволит использовать новые функции, такие как 'Обновление EPUB'.\n\n"
            "<b>Рекомендуется сделать резервную копию папки перед миграцией.</b>\n\n"
            "Выполнить миграцию?"
        )

        migrate_button = msg_box.addButton("Да, мигрировать", QMessageBox.ButtonRole.YesRole)
        cancel_button = msg_box.addButton("Нет, пропустить", QMessageBox.ButtonRole.NoRole)

        msg_box.exec()

        if msg_box.clickedButton() == migrate_button:
            moved, errors = migrator.run_migration()

            summary_message = f"Миграция завершена.\n\n- Успешно перемещено и зарегистрировано: {moved}\n- Ошибок (файлы оставлены на месте): {errors}"

            if errors > 0:
                QMessageBox.warning(self, "Миграция завершена с ошибками", summary_message)
            else:
                QMessageBox.information(self, "Миграция завершена успешно", summary_message)


    def _copy_original_chapters(self):
        """
        Копирует оригиналы выбранных глав, управляя пакетной обработкой
        для замены терминов по глоссарию и обновляя статус задач.
        """
        selected_rows = {item.row() for item in self.task_management_widget.chapter_list_widget.table.selectedItems()}
        if not selected_rows:
            self._show_custom_message("Нет выбора", "Пожалуйста, выберите задачи в списке.", QMessageBox.Icon.Information)
            return

        if not all([self.selected_file, self.output_folder, self.project_manager]):
            self._show_custom_message("Ошибка проекта", "Для операции нужен EPUB и папка проекта.", QMessageBox.Icon.Warning)
            return

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Способ копирования")
        msg_box.setIcon(QMessageBox.Icon.Question)
        msg_box.setText("Как скопировать оригиналы выбранных глав?")
        msg_box.setInformativeText(
            "<b>'Скопировать как есть'</b>: Создает точную копию исходного файла.\n\n"
            "<b>'Обработать по глоссарию'</b>: Находит в тексте термины из глоссария и заменяет их на переводы. Полезно для подготовки к ручному переводу."
        )

        btn_as_is = msg_box.addButton("Скопировать как есть", QMessageBox.ButtonRole.ActionRole)
        btn_process = msg_box.addButton("Обработать по глоссарию", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)

        glossary_list = self.glossary_widget.get_glossary()
        if not glossary_list:
            btn_process.setEnabled(False)
            btn_process.setToolTip("Кнопка неактивна, так как глоссарий проекта пуст.")

        msg_box.exec()
        clicked_button = msg_box.clickedButton()

        if clicked_button == btn_as_is:
            process_with_glossary = False
            mode_text = "(копии оригиналов)"
        elif clicked_button == btn_process:
            process_with_glossary = True
            mode_text = "(обработано по глоссарию)"
        else:
            return

        provider_id = self.key_management_widget.get_selected_provider()
        provider_config = api_config.api_providers().get(provider_id, {})
        file_suffix = provider_config.get('file_suffix')

        if not file_suffix:
            self._show_custom_message("Ошибка конфигурации", f"Не удалось определить суффикс для провайдера '{provider_id}'.", QMessageBox.Icon.Critical)
            return

        selected_tasks = []
        chapters_to_process = set()
        for row in selected_rows:
            task_item = self.task_management_widget.chapter_list_widget.table.item(row, 0)
            if not task_item: continue

            task_tuple = task_item.data(QtCore.Qt.ItemDataRole.UserRole)
            selected_tasks.append(task_tuple)

            task_type = task_tuple[1][0]
            if task_type in ('epub', 'epub_chunk'):
                chapters_to_process.add(task_tuple[1][2])
            elif task_type == 'epub_batch':
                chapters_to_process.update(task_tuple[1][2])

        if not chapters_to_process:
            self._show_custom_message("Нечего обрабатывать", "Выбранные задачи не содержат глав.", QMessageBox.Icon.Warning)
            return

        replacer = None
        if process_with_glossary:
            full_glossary_data = {}
            for entry in glossary_list:
                original = str(entry.get('original') or "").strip()
                if not original:
                    continue
                full_glossary_data[original] = {
                    'rus': str((entry.get('rus') or entry.get('translation')) or ""),
                    'note': str(entry.get('note') or "")
                }
            if full_glossary_data:
                replacer = GlossaryReplacer(full_glossary_data)

        copied_count, skipped_count, errors = 0, 0, []
        successfully_processed_chapters = set()

        try:
            if replacer:
                replacer.prepare()

            with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as epub_zip:
                for chapter_path in chapters_to_process:
                    try:
                        full_dest_path = build_translated_output_path(
                            self.output_folder,
                            chapter_path,
                            file_suffix,
                        )
                        os.makedirs(os.path.dirname(full_dest_path), exist_ok=True)

                        if os.path.exists(full_dest_path):
                            skipped_count += 1
                        else:
                            html_str = epub_zip.read(chapter_path).decode('utf-8', 'ignore')
                            content_to_write = (replacer.process_html(html_str).encode('utf-8') if replacer else html_str.encode('utf-8'))

                            with open(full_dest_path, 'wb') as f:
                                f.write(content_to_write)
                            copied_count += 1

                        relative_path = os.path.relpath(full_dest_path, self.output_folder)
                        self.project_manager.register_translation(chapter_path, file_suffix, relative_path)

                        successfully_processed_chapters.add(chapter_path)
                    except Exception as e:
                        errors.append(f"Ошибка для главы '{chapter_path}': {e}")
        except Exception as e:
            self._show_custom_message("Критическая ошибка обработки", f"Произошла ошибка во время пакетной обработки: {e}", QMessageBox.Icon.Critical)
            return
        finally:
            if replacer:
                replacer.cleanup()

        for task_tuple in selected_tasks:
            task_type = task_tuple[1][0]
            chapters_in_task = []
            if task_type in ('epub', 'epub_chunk'):
                chapters_in_task.append(task_tuple[1][2])
            elif task_type == 'epub_batch':
                chapters_in_task.extend(task_tuple[1][2])

            if all(ch in successfully_processed_chapters for ch in chapters_in_task):
                self.task_manager.task_done("UI_ACTION", task_tuple)

        total_processed = copied_count + skipped_count
        summary_text = f"Успешно обработано {total_processed} глав {mode_text}:"
        informative_text = f"- Скопировано новых: {copied_count}\n- Пропущено (уже существуют): {skipped_count}"

        if errors:
            informative_text += f"\n\nПроизошли ошибки ({len(errors)}):\n" + "\n".join(errors[:3])
            self._show_custom_message("Завершено с ошибками", summary_text, QMessageBox.Icon.Warning, informative_text, button_text="Принял")
        else:
            self._show_custom_message("Готово", summary_text, QMessageBox.Icon.Information, informative_text, button_text="Отлично")

    def _get_full_ui_settings(self):
        """Собирает полный 'слепок' настроек из всех релевантных виджетов (БЕЗ глоссария)."""
        settings = self.get_settings()

        settings.update(self.translation_options_widget.get_settings())
        settings['auto_translation'] = self.auto_translate_widget.get_settings()
        settings[THEME_SETTINGS_KEY] = editable_theme_colors(getattr(self, '_ui_theme_colors', None))
        settings['active_keys_by_provider'] = {
            provider_id: sorted(list(keys))
            for provider_id, keys in self.key_management_widget.current_active_keys_by_provider.items()
            if keys
        }

        # Удаляем данные, которые не должны сохраняться как "настройки"
        settings.pop('selected_chapters', None)
        settings.pop('file_path', None)
        settings.pop('output_folder', None)
        settings.pop('full_glossary_data', None)
        settings.pop('project_manager', None)

        return settings


    def _apply_full_ui_settings(self, settings: dict):
        """
        Применяет полный 'слепок' настроек ко всем виджетам (БЕЗ глоссария),
        блокируя сигналы, чтобы избежать ложного 'загрязнения' состояния.
        """
        if not settings:
            print("[INFO] Нет сохраненных настроек сессии для применения.")
            return

        # --- Блокируем сигналы, чтобы избежать ложного срабатывания is_settings_dirty ---
        self.model_settings_widget.blockSignals(True)
        self.translation_options_widget.blockSignals(True)
        self.preset_widget.blockSignals(True)
        self.key_management_widget.blockSignals(True)
        self.instances_spin.blockSignals(True)
        self.auto_translate_widget.blockSignals(True)

        try:
            if THEME_SETTINGS_KEY in settings:
                self._apply_ui_theme_colors(extract_theme_colors(settings), mark_dirty=False)

            self.model_settings_widget.set_settings(settings)
            if any(key in settings for key in (
                'use_batching',
                'chunking',
                'chunk_on_error',
                'sequential_translation',
                'sequential_translation_splits',
                'task_size_limit',
            )):
                self.translation_options_widget.set_settings(settings)

            auto_translation_settings = settings.get('auto_translation')
            if isinstance(auto_translation_settings, dict):
                self.auto_translate_widget.set_settings(auto_translation_settings)

            if 'custom_prompt' in settings:
                self.preset_widget.set_prompt(settings['custom_prompt'])

            model_name = settings.get('model')
            model_id = api_config.all_models().get(model_name, {}).get('id')
            if model_id:
                self.key_management_widget.set_current_model(model_id)

            active_keys_by_provider = settings.get('active_keys_by_provider')
            if isinstance(active_keys_by_provider, dict):
                for provider_id, active_keys in active_keys_by_provider.items():
                    if not provider_id:
                        continue
                    normalized_keys = [
                        key for key in active_keys
                        if isinstance(key, str) and key.strip()
                    ]
                    self.key_management_widget.current_active_keys_by_provider[provider_id] = set(normalized_keys)

            provider_id = settings.get('provider')
            active_keys = settings.get('api_keys', [])
            if not isinstance(active_keys, (list, tuple, set)):
                active_keys = []
            if provider_id:
                self.key_management_widget.set_active_keys_for_provider(provider_id, active_keys)
            else:
                self.key_management_widget._load_and_refresh_keys()

            self._update_instances_spinbox_limit()
            saved_instances = settings.get('num_instances')
            if saved_instances is not None:
                try:
                    saved_instances = int(saved_instances)
                except (TypeError, ValueError):
                    saved_instances = 1
                saved_instances = max(1, min(saved_instances, self.instances_spin.maximum()))
                self.instances_spin.setValue(saved_instances)
        finally:
            # --- Обязательно разблокируем сигналы в блоке finally ---
            self.model_settings_widget.blockSignals(False)
            self.translation_options_widget.blockSignals(False)
            self.preset_widget.blockSignals(False)
            self.key_management_widget.blockSignals(False)
            self.instances_spin.blockSignals(False)
            self.auto_translate_widget.blockSignals(False)

        self._refresh_auto_translate_runtime_context()
        self._update_distribution_info_from_widget()
        self.check_ready()

        print("[INFO] Настройки сессии успешно применены к UI.")

    def _write_snapshot_ui_settings(self, snapshot_path: str, settings: dict):
        """Сохраняет UI-состояние в метаданные snapshot-файла очереди."""
        if not snapshot_path or not os.path.exists(snapshot_path) or not settings:
            return

        conn = None
        try:
            payload = json.dumps(settings, ensure_ascii=False)
            conn = sqlite3.connect(snapshot_path)
            conn.execute("CREATE TABLE IF NOT EXISTS meta_info (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO meta_info (key, value) VALUES (?, ?)",
                ("ui_session_settings", payload)
            )
            conn.commit()
        except Exception as exc:
            print(f"[WARN] Не удалось сохранить UI-состояние в snapshot: {exc}")
        finally:
            if conn:
                conn.close()

    def _read_snapshot_ui_settings(self, snapshot_path: str) -> dict:
        """Читает UI-состояние из метаданных snapshot-файла очереди."""
        if not (self.engine and self.engine.task_manager):
            return {}

        meta = self.engine.task_manager.read_queue_snapshot_meta(snapshot_path) or {}
        raw_settings = meta.get('ui_session_settings')
        if not raw_settings:
            return {}

        try:
            settings = json.loads(raw_settings)
        except (TypeError, json.JSONDecodeError) as exc:
            print(f"[WARN] Не удалось прочитать UI-состояние из snapshot: {exc}")
            return {}

        return settings if isinstance(settings, dict) else {}


    def _save_project_settings_only(self):
        """Сохраняет только настройки UI в файл проекта."""
        if not self.output_folder: return

        project_settings_path = os.path.join(self.output_folder, "project_settings.json")
        manager_to_save = SettingsManager(config_file=project_settings_path)
        manager_to_save.save_full_session_settings(self._get_full_ui_settings())

        self.is_settings_dirty = False
        self._refresh_dirty_window_title()
        print("[SETTINGS] Настройки проекта сохранены.")





    def _save_project_glossary_only(self):
        """Сохраняет только глоссарий в файл проекта и обновляет 'чистое' состояние."""
        if not self.output_folder: return

        project_glossary_path = os.path.join(self.output_folder, "project_glossary.json")
        current_glossary = self.glossary_widget.get_glossary()
        try:
            with open(project_glossary_path, 'w', encoding='utf-8') as f:
                json.dump(current_glossary, f, ensure_ascii=False, indent=2, sort_keys=True)

            self.mark_project_glossary_as_saved(current_glossary)

            print("[SETTINGS] Глоссарий проекта сохранен.")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить глоссарий проекта: {e}")

    def _save_project_data(self):
        """
        Сохраняет ВСЕ данные проекта: и настройки UI, и глоссарий.
        """
        if not self.output_folder:
            return
        self._save_project_settings_only()
        self._save_project_glossary_only()

    def check_ready(self):
        """
        Проверяет, все ли условия выполнены для запуска, и обновляет
        состояние и СТИЛЬ всех кнопок управления.
        Версия 2.6: Раздельная логика для перевода и генерации глоссария.
        """

        # --- НОВАЯ ЗАЩИТА: Синхронизация с реальностью ---
        if self._check_and_sync_active_session():
            # Если мы обнаружили активную сессию, UI уже заблокирован внутри метода синхронизации.
            # Нам не нужно проверять валидность полей для старта. Выходим.
            return

        if self.is_session_active:
            return

        if self._auto_followup_running or self._auto_glossary_running:
            self.start_btn.setEnabled(False)
            return

        # --- Условие для основного перевода (требует ключи) ---
        num_active_keys = len(self.key_management_widget.get_active_keys())
        can_start_translation = all([
            self.selected_file,
            self.output_folder,
            self.html_files,
            num_active_keys > 0
        ])

        self.start_btn.setEnabled(can_start_translation)

        # --- Условие для генерации глоссария (НЕ требует ключи здесь) ---
        can_generate_glossary = bool(self.selected_file and self.output_folder and self.html_files)
        self.glossary_widget.set_generation_enabled(can_generate_glossary)

        # --- Остальные проверки ---
        can_dry_run = bool(self.selected_file and self.html_files)
        self.dry_run_btn.setEnabled(can_dry_run)

        can_validate_or_build = bool(self.selected_file and self.output_folder)
        self.task_management_widget.set_validation_enabled(can_validate_or_build)
        self.project_actions_widget.set_build_epub_enabled(can_validate_or_build)
        self.project_actions_widget.set_sync_enabled(can_validate_or_build)

        if hasattr(self, 'instances_spin'):
            self._update_distribution_info_from_widget()

    def _run_project_sync(self):
        """Запускает синхронизацию проекта в фоновом потоке."""
        if not self.project_manager: return

        from ...utils.project_migrator import ProjectMigrator, SyncThread

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Синхронизация")
        self.wait_dialog.setText("Идет анализ проекта…")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        migrator = ProjectMigrator(self.output_folder, self.selected_file, self.project_manager)

        self.sync_thread = SyncThread(migrator, parent_widget=self)
        self.sync_thread.finished_sync.connect(self._on_sync_finished)

        self.sync_thread.start()
        self.wait_dialog.show()

    def _on_sync_finished(self, is_project_ready, message):
        """Обрабатывает результат фоновой синхронизации."""
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            return

        QMessageBox.information(self, "Синхронизация", message)

        # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        # Вместо полной перезагрузки проекта, мы вызываем наш "оркестратор",
        # который обновит список задач и UI на основе свежих данных,
        # не заставляя пользователя заново выбирать главы.
        self._on_project_data_changed()

    def _update_instances_spinbox_limit(self):
        """
        Этот слот вызывается ТОЛЬКО при изменении списка активных сессий в UI.
        Он корректно обновляет максимум для spinbox'а, защищая значение пользователя.
        """
        if self.is_session_active:
            return # Не трогаем spinbox во время активной сессии!

        session_capacity = self._get_available_session_capacity()

        # Устанавливаем новый максимум. QSpinBox АВТОМАТИЧЕСКИ уменьшит текущее значение,
        # если оно больше максимума. Нам не нужно делать это вручную через setValue,
        # так как это может сбить "память" виджета при кратковременных просадках максимума.
        self.instances_spin.setMaximum(session_capacity if session_capacity > 0 else 1)

        # Обновляем текстовую метку с распределением, так как она тоже зависит от этого.
        self._update_distribution_info_from_widget()

    def _filter_all_translated_chapters(self, silent=False):
        """
        Фильтрует self.html_files, оставляя только те главы, для которых НЕТ
        ни одной версии перевода в карте проекта.
        """
        if not self.project_manager or not self.html_files:
            return

        chapters_to_keep = [ch for ch in self.html_files if not self.project_manager.get_versions_for_original(ch)]

        # Если список не изменился, ничего не делаем
        if len(chapters_to_keep) == len(self.html_files):
            if not silent: QMessageBox.information(self, "Нет изменений", "В текущем списке нет переведенных глав.")
            return

        # Если после фильтрации ничего не осталось
        if not chapters_to_keep and not silent:
            QMessageBox.information(self, "Все переведено", "Все выбранные главы уже имеют хотя бы одну версию перевода. Список будет очищен.")

        # Обновляем основной список глав
        self.html_files = chapters_to_keep

        # Показываем сообщение, только если мы не в "тихом" режиме
        if not silent:
            if chapters_to_keep:
                QMessageBox.information(self, "Готово", f"Список отфильтрован. Скрыты все переведенные главы. Осталось: {len(self.html_files)}.")
            # Обновляем UI, так как это был прямой вызов от пользователя
            self._on_project_data_changed()

    def _ensure_pending_tasks_for_start(self) -> bool:
        """
        Guarantees that the internal task queue matches the selected chapters
        before start validation runs.
        """
        engine = getattr(self, 'engine', None)
        task_manager = getattr(engine, 'task_manager', None) or getattr(self, 'task_manager', None)
        if task_manager and task_manager.has_pending_tasks():
            return True

        if not (self.selected_file and self.output_folder and self.html_files):
            return False

        print("[INFO] Очередь задач пуста перед стартом. Пересобираю её из выбранных глав…")
        self._prepare_and_display_tasks(clean_rebuild=True)
        engine = getattr(self, 'engine', None)
        task_manager = getattr(engine, 'task_manager', None) or getattr(self, 'task_manager', None)
        return bool(task_manager and task_manager.has_pending_tasks())


    def _start_translation(
        self,
        checked=False,
        is_auto_restart: bool = False,
        skip_auto_glossary: bool = False,
        preserve_log: bool = False,
    ):
        """
        Собирает настройки и отправляет команду на запуск сессии.
        """

        if self._check_and_sync_active_session():
            # Если метод вернул True, значит сессия УЖЕ шла.
            # Мы только что обновили UI (включили Стоп, выключили Старт).
            # Просто выходим, не отправляя команду повторно.
            print("[INFO] Нажатие 'Старт' проигнорировано: сессия уже активна (интерфейс обновлен).")
            return

        auto_settings = self.auto_translate_widget.get_settings() if hasattr(self, 'auto_translate_widget') else {}
        pending_session_override = (
            dict(self._auto_restart_session_override)
            if is_auto_restart and isinstance(self._auto_restart_session_override, dict)
            else None
        )
        (
            auto_translation_options_override,
            auto_translation_mode,
            auto_has_translation_override,
            auto_batch_token_limit,
            auto_batch_char_limit,
            auto_batch_profile,
        ) = self._resolve_auto_translation_options(auto_settings)
        auto_model_name, auto_model_config, auto_model_warning = self._resolve_auto_model_override(auto_settings)
        if auto_settings.get('enabled') and not is_auto_restart and auto_has_translation_override:
            self._prepare_and_display_tasks(
                clean_rebuild=False,
                translation_options_override=auto_translation_options_override,
            )
            if auto_translation_mode != 'inherit':
                mode_titles = {
                    'batch': "пакетами",
                    'single': "по одной главе",
                    'chunk': "чанками",
                }
                self._auto_log(
                    f"Основной автоперевод будет собран {mode_titles.get(auto_translation_mode, auto_translation_mode)}.",
                    force=True
                )
            if auto_batch_token_limit > 0 and auto_batch_char_limit:
                self._auto_log(
                    "Лимит пакета для основного автопрогона: "
                    f"~{auto_batch_token_limit} входных токенов "
                    f"(≈{auto_batch_char_limit} символов, профиль: {auto_batch_profile}).",
                    force=True
                )
        if auto_settings.get('enabled') and not is_auto_restart and auto_model_warning:
            self._auto_log(f"{auto_model_warning} Использую модель из общих настроек.", force=True)
        if auto_settings.get('enabled') and not is_auto_restart and auto_model_name:
            self._auto_log(f"Модель основного автопрогона: {auto_model_name}.", force=True)

        # 1. Проверяем и при необходимости восстанавливаем очередь задач.
        tasks_exist = self._ensure_pending_tasks_for_start()
        active_keys_for_start = (
            pending_session_override.get('api_keys')
            if isinstance(pending_session_override, dict) and pending_session_override.get('api_keys')
            else self.key_management_widget.get_active_keys()
        )

        # 2. Проверяем все условия для старта
        if not all([self.selected_file, tasks_exist, self.output_folder, active_keys_for_start]):
            QMessageBox.warning(self, "Ошибка", "Необходимо выбрать файл, задачи, папку и активную сессию сервиса.")
            return

        if self.engine and self.engine.task_manager:
            self.engine.task_manager.release_held_tasks()

        # 3. Получаем настройки. В них больше нет 'selected_chapters'.
        settings = self.get_settings()
        auto_settings = settings.get('auto_translation', {})
        if auto_settings.get('enabled') and auto_has_translation_override:
            settings.update(auto_translation_options_override)
        if auto_settings.get('enabled') and auto_model_name and auto_model_config:
            settings['model'] = auto_model_name
            settings['model_config'] = auto_model_config
        else:
            settings['model_config'] = api_config.all_models().get(settings.get('model'))
        self._apply_auto_thinking_override(settings, auto_settings, model_config=settings.get('model_config'))
        if pending_session_override:
            settings.update(pending_session_override)
            if not settings.get('model_config'):
                settings['model_config'] = api_config.all_models().get(settings.get('model'))

        session_model_id = (settings.get('model_config') or {}).get('id')
        if session_model_id:
            self.key_management_widget.set_current_model(session_model_id)

        # 4. Проверяем существование файла (эта проверка остается важной)
        original_epub_path = settings.get('file_path')
        if not original_epub_path or not os.path.exists(original_epub_path):
            QMessageBox.critical(self, "Критическая ошибка: Файл не найден", f"Не удалось найти исходный EPUB файл: {original_epub_path}")
            self.selected_file = None
            self.html_files = []
            self.paths_widget.set_file_path(None)
            self.check_ready()
            return

        # 5. Сохраняем все релевантные настройки перед запуском
        if not is_auto_restart and not preserve_log:
            self.log_widget.clear()
        self.settings_manager.add_to_project_history(self.selected_file, self.output_folder)
        self.settings_manager.save_custom_prompt(self.preset_widget.get_prompt())
        self.settings_manager.save_last_prompt_preset_name(self.preset_widget.get_current_preset_name())
        self.auto_translate_widget.save_last_state_now()
        if self.local_set:
            self._save_project_settings_only()
        else:
            self._save_global_ui_settings()
        if self.glossary_widget.get_glossary() != self.initial_glossary_state:
            self._save_project_glossary_only()

        if (
            not is_auto_restart
            and not skip_auto_glossary
            and auto_settings.get('enabled')
            and auto_settings.get('glossary_enabled')
        ):
            self._start_auto_glossary_then_translation(settings, auto_settings)
            return

        if not is_auto_restart:
            if auto_settings.get('enabled'):
                self._auto_workflow_enabled_for_session = True
                self._auto_workflow_round = 0
                self._auto_last_retry_signatures = set()
            else:
                self._reset_auto_workflow_state()
        else:
            self._auto_followup_running = False

        # 6. Отправляем событие на запуск сессии
        self.this_dialog_started_the_session = True
        self._auto_restart_session_override = None
        self._post_event(name='start_session_requested', data={'settings': settings})

        # 7. Обновляем UI
        self.start_btn.setEnabled(False)
        if is_auto_restart:
            self._auto_log(f"Перезапускаю перевод. Текущий автоцикл: {self._auto_workflow_round}.", force=True)
        else:
            self._post_event('log_message', {'message': "[SYSTEM] Команда на запуск сессии отправлена…"})


    def _stop_translation(self):
        """
        Отправляет команду на остановку сессии через шину событий.
        """
        if self.engine and self.engine.session_id:
            if self._hard_stop_enabled:
                self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на немедленную остановку сессии…"})
                self._post_event('manual_stop_requested')
            else:
                self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на плавную остановку сессии…"})
                self._post_event('soft_stop_requested')
                self._set_stop_button_mode(True)

        elif self._check_and_sync_active_session():
            if self._hard_stop_enabled:
                self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на немедленную остановку сессии…"})
                self._post_event('manual_stop_requested')
            else:
                self._post_event('log_message', {'message': "[SYSTEM] Отправка запроса на плавную остановку сессии…"})
                self._post_event('soft_stop_requested')
                self._set_stop_button_mode(True)

    @pyqtSlot()
    def _on_session_finished(self):
        """
        Финальная процедура очистки UI. "Размораживает" задачи после dry_run.
        """
        try:
            # --- ИСПРАВЛЕНИЕ: Вместо восстановления, просто "размораживаем" ---
            if self.engine and self.engine.task_manager:
                self.engine.task_manager.release_held_tasks()

            # --- Кнопка dry_run теперь сбрасывается всегда ---
            self.dry_run_btn.setText("Пробный запуск")

            self._post_event('log_message', {'message': "[SYSTEM] Получен сигнал завершения. Очистка интерфейса…"})
            if self.project_manager:
                self.project_manager.reload_data_from_disk()
                print("[INFO] Карта проекта обновлена после завершения сессии.")

            if self.output_folder:
                self.project_manager = TranslationProjectManager(self.output_folder)

            self.key_management_widget._load_and_refresh_keys()
            self.task_management_widget.check_and_update_retry_button_visibility()
            self.status_bar.stop_session()
            self._set_stop_button_mode(False)
            self._set_controls_enabled(True)

            # После завершения сессии синхронизируем стили ключей с выбранной моделью UI.
            try:
                current_ui_model_name = self.model_settings_widget.model_combo.currentText()
                model_config = api_config.all_models().get(current_ui_model_name, {})
                model_id_to_sync = model_config.get('id')
                if model_id_to_sync:
                    self.key_management_widget.set_current_model(model_id_to_sync)
                    print(f"[INFO] Синхронизация статусов ключей для модели: {current_ui_model_name} ({model_id_to_sync})")
            except Exception as e:
                print(f"[ERROR] Не удалось синхронизировать виджет ключей после сессии: {e}")

        except Exception as e:
            error_text = f"[SESSION FINISH UI ERROR] {type(e).__name__}: {e}"
            print(error_text)
            try:
                self._post_event('log_message', {'message': error_text})
            except Exception:
                pass

            # Даже если один из виджетов не смог обновиться, не даем приложению
            # упасть в самом конце перевода: освобождаем базовый UI-контур.
            try:
                self.status_bar.stop_session()
            except Exception:
                pass
            try:
                self._set_stop_button_mode(False)
                self._set_controls_enabled(True)
            except Exception:
                pass

        QtCore.QMetaObject.invokeMethod(
            self, "_finalize_session_state",
            QtCore.Qt.ConnectionType.QueuedConnection
        )

    @pyqtSlot()
    def _finalize_session_state(self):
        """Этот слот вызывается асинхронно для безопасного сброса флага сессии."""
        try:
            reason = getattr(self, '_shutdown_reason', '')
            self.is_session_active = False
            self._snapshot_save_timer.stop()
            self._save_snapshot_async(force=True)
            self._post_event('log_message', {'message': "[SYSTEM] Интерфейс полностью разблокирован."})
            self.check_ready() # Теперь вызываем проверку, когда флаг точно сброшен

            # Проверяем, был ли это последний воркер и была ли сессия остановлена принудительно
            if hasattr(self, '_shutdown_reason') and hasattr(self, '_log_session_id'):
                session_id_log = self._log_session_id
                QtCore.QTimer.singleShot(
                    100,
                    lambda: post_session_separator(self._post_event, session_id_log=session_id_log, reason=reason),
                )

            self._schedule_auto_workflow_followup(reason)

        except Exception as e:
            error_text = f"[FINALIZE SESSION UI ERROR] {type(e).__name__}: {e}"
            print(error_text)
            try:
                self._post_event('log_message', {'message': error_text})
            except Exception:
                pass
        finally:
            if hasattr(self, '_shutdown_reason'):
                del self._shutdown_reason
            if hasattr(self, '_log_session_id'):
                del self._log_session_id


    def _open_filter_packaging_dialog(self):
        """
        Открывает диалог для умной пакетной подготовки отфильтрованных глав.
        Версия 2.1: Исправлен поиск задач (теперь ищет 'error' + 'CONTENT_FILTER').
        """
        if not (self.engine and self.engine.task_manager):
            QMessageBox.information(self, "Нет данных", "Менеджер задач не инициализирован.")
            return

        # 1. Получаем ПОЛНЫЙ список состояния задач
        all_tasks_state = self.engine.task_manager.get_ui_state_list()

        filtered_chapters = set()
        successful_chapters = set()

        successful_map = {}
        if self.project_manager:
            for original, versions in self.project_manager.get_full_map().items():
                for suffix, rel_path in versions.items():
                    if suffix != 'filtered':
                        full_path = os.path.join(self.project_manager.project_folder, rel_path)
                        if os.path.exists(full_path):
                            successful_map[original] = full_path
                            break

        # 2. Итерируемся по актуальному состоянию
        # ВАЖНО: распаковываем details (третий элемент), чтобы проверить ошибки
        for task_info, status, details in all_tasks_state:
            payload = task_info[1]
            chapters_in_task = []
            if payload[0] in ('epub', 'epub_chunk'):
                chapters_in_task.append(payload[2])
            elif payload[0] == 'epub_batch':
                chapters_in_task.extend(payload[2])

            # Проверяем наличие ошибки CONTENT_FILTER в деталях задачи
            is_filtered = (status == 'error' and 'CONTENT_FILTER' in details.get('errors', {}))

            for chapter in chapters_in_task:
                if is_filtered:
                    filtered_chapters.add(chapter)
                elif status == 'success' and chapter in successful_map:
                    successful_chapters.add(chapter)

        if not filtered_chapters:
            QMessageBox.information(self, "Нет данных", "Не найдено задач, остановленных фильтром контента.")
            return

        # 3. Получаем рекомендуемый размер из виджета опций
        recommended_size = self.translation_options_widget.task_size_spin.value()

        real_chapter_sizes = {
            path: composition.get('total_size', 0)
            for path, composition in self.translation_options_widget.chapter_compositions.items()
        }

        if not real_chapter_sizes:
             QMessageBox.warning(self, "Ошибка", "Не удалось получить данные о размерах глав. Попробуйте перезагрузить проект.")
             return

        # 4. Создаем и запускаем диалог
        dialog = FilterPackagingDialog(
            filtered_chapters=list(filtered_chapters),
            successful_chapters=list(successful_chapters),
            recommended_size=recommended_size,
            epub_path=self.selected_file,
            real_chapter_sizes=real_chapter_sizes,
            parent=self
        )

        if dialog.exec():
            result = dialog.get_result()
            if result:
                self._process_filter_dialog_result(result)

    def _get_filter_retry_translation_options(self) -> dict:
        """
        Filter retries should follow the visible chunk checkboxes only.
        Passing this as an explicit override also prevents auto-translation
        mode overrides from silently rebuilding filter retries as chunks.
        """
        widget = getattr(self, 'translation_options_widget', None)
        get_settings = getattr(widget, 'get_settings', None)
        if callable(get_settings):
            options = get_settings().copy()
        else:
            options = {}
        options.setdefault('chunking', False)
        options.setdefault('chunk_on_error', False)
        return options

    def _process_filter_dialog_result(self, result: dict):
        """
        Обрабатывает результат из FilterPackagingDialog.
        Версия 2.1: Добавляет искусственную историю ошибок (2x CONTENT_FILTER)
        для новых пакетов, чтобы форсировать атомарный режим генерации.
        """
        result_type = result.get('type')
        data = result.get('data')

        if not data:
            data = []

        plain_payloads = []

        # Создаем "прививку" от фильтров: 2 ошибки CONTENT_FILTER
        # Это сигнал для воркера использовать безопасный (атомарный) режим.
        artificial_history = {'errors': {'CONTENT_FILTER': 2}}

        if result_type == 'chapters':
            # Тип 1: Список глав. Отправляем в TaskPreparer через штатный метод.
            # В этом случае мы не можем легко внедрить историю, так как TaskPreparer внутри.
            # Но обычно диалог фильтрации возвращает payloads (Тип 2).
            self.html_files = data
            self._prepare_and_display_tasks(
                clean_rebuild=True,
                translation_options_override=self._get_filter_retry_translation_options(),
            )

        elif result_type == 'payloads':
            # Тип 2: Готовые пейлоады.
            plain_payloads = data

            # Обновляем UI счетчик глав
            all_chapters_in_payloads = set()
            for payload in plain_payloads:
                metadata = payload[3] if len(payload) > 3 and isinstance(payload[3], dict) else {}
                save_chapters = metadata.get('save_chapters')
                if save_chapters:
                    all_chapters_in_payloads.update(save_chapters)
                elif payload[0] == 'epub_batch':
                    all_chapters_in_payloads.update(payload[2])
                elif payload[0] in ('epub', 'epub_chunk') and len(payload) > 2:
                    all_chapters_in_payloads.add(payload[2])

            self.html_files = sorted(list(all_chapters_in_payloads), key=extract_number_from_path)
            self.paths_widget.update_chapters_info(len(self.html_files))

            # Напрямую перезаписываем очередь в TaskManager с ВАКЦИНАЦИЕЙ
            self.task_manager.set_pending_tasks(plain_payloads, initial_history=artificial_history)
            self.translation_options_widget._update_info_text()

        # Общие действия после обработки
        self._post_event('log_message', {'message': f"[INFO] Сформированы задачи для обхода фильтров. Активирован безопасный режим (Content Filter x2); авто-чанкинг фильтра не включается."})
        self.task_management_widget.set_retry_filtered_button_visible(False)


    def _set_controls_enabled(self, enabled):
        """
        Централизованно включает/выключает все элементы управления на время перевода.
        """
        is_session_active = not enabled

        # Кнопки Старт/Стоп
        self.start_btn.setEnabled(not is_session_active)
        self.stop_btn.setEnabled(is_session_active)

        # Эти виджеты блокируются полностью
        widgets_to_toggle = [
            self.paths_widget,
            self.key_management_widget,
            self.glossary_widget,
            self.preset_widget,
            self.auto_translate_widget,
            self.translation_options_widget,
            self.model_settings_widget,
            self.project_actions_widget,
            self.dry_run_btn,
        ]
        for widget in widgets_to_toggle:
            widget.setEnabled(not is_session_active)

        # А этот виджет переводится в специальный режим
        self.task_management_widget.set_session_mode(is_session_active)

        if not enabled:
            # Сессия НАЧАЛАСЬ
            self._set_stop_button_mode(self._hard_stop_enabled)
        else:
            # Сессия ЗАВЕРШИЛАСЬ
            self._set_stop_button_mode(False)
            self.dry_run_btn.setText("Пробный запуск")


    @pyqtSlot(str, object, bool, str, str, str)
    def _on_chapter_status_update(self, session_id, task_info_result, success, err_type, msg, final_status):
        """Обновляет статус задачи в UI."""
        task_info, _ = (task_info_result, None)
        if isinstance(task_info_result, tuple) and len(task_info_result) == 2:
            task_info, _ = task_info_result

        self.task_management_widget.update_task_status(task_info, final_status)

        # Обновляем счетчики
        self.status_bar.increment_status(final_status)


    # --- НОВЫЙ МЕТОД ДЛЯ ПРИЕМА ДАННЫХ ИЗ ВАЛИДАТОРА ---
    def add_files_for_retry(self, epub_path, chapter_paths):
        """
        Принимает список глав из Валидатора, полностью заменяет
        текущий список задач и обновляет весь UI.
        """
        if self.selected_file != epub_path:
            QMessageBox.warning(self, "Конфликт проектов",
                                "Главы для повтора относятся к другому EPUB файлу. "
                                "Пожалуйста, сначала загрузите соответствующий проект.")
            return

        # 1. Заменяем текущий список выбранных глав на новый
        self.html_files = chapter_paths

        # 2. Логируем действие
        self._post_event('log_message', {'message': f"[INFO] Загружено {len(chapter_paths)} глав для повторного перевода из Валидатора."})

        # 3. Полностью обновляем UI на основе нового списка глав
        self._on_project_data_changed()

        # Перепроверяем готовность к запуску
        self.check_ready()


    def _open_project_history(self):
        """Открывает диалог с историей проектов."""
        history = self.settings_manager.load_project_history()
        if not history:
            QMessageBox.information(self, "История пуста", "Вы еще не запускали ни одного перевода.")
            return

        # Передаем settings_manager в диалог
        dialog = ProjectHistoryDialog(history, self.settings_manager, self)

        if dialog.exec():
            # Эта часть кода сработает, только если пользователь выбрал проект
            # и нажал "Загрузить". Удаление уже было сохранено внутри диалога.
            selected_project = dialog.get_selected_project()
            if selected_project:
                self._load_project(selected_project)

    def _resolve_project_epub_path(self, project_data):
        output_folder = project_data.get("output_folder")
        epub_path = project_data.get("epub_path")

        if epub_path and os.path.exists(epub_path):
            return epub_path

        guessed_epubs = []
        if output_folder and os.path.isdir(output_folder):
            try:
                guessed_epubs = sorted(
                    os.path.join(output_folder, name)
                    for name in os.listdir(output_folder)
                    if name.lower().endswith(".epub")
                )
            except OSError:
                guessed_epubs = []

        if len(guessed_epubs) == 1:
            epub_path = guessed_epubs[0]
        else:
            start_dir = output_folder if output_folder and os.path.isdir(output_folder) else ""
            epub_path, _ = QFileDialog.getOpenFileName(
                self,
                "Выберите исходный EPUB для проекта",
                start_dir,
                "EPUB files (*.epub)"
            )

        if not epub_path:
            return None

        self.settings_manager.add_to_project_history(epub_path, output_folder)
        project_data["epub_path"] = epub_path
        return epub_path

    def _load_project(self, project_data):
        """
        Загружает проект из истории. Устанавливает пути, загружает глоссарий
        и запускает процесс выбора глав.
        Версия 2.0: Добавлена логика сброса состояния при смене проекта.
        """
        epub_path = project_data.get("epub_path")
        output_folder = project_data.get("output_folder")

        if not os.path.isdir(output_folder):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Пути не найдены")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setText(f"Не удалось найти файл или папку для проекта '{project_data.get('name')}'.")
            msg_box.setInformativeText(f"Файл: {epub_path}\nПапка: {output_folder}\n\nУдалить эту некорректную запись из истории?")
            yes_button = msg_box.addButton("Да, удалить", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.exec()
            if msg_box.clickedButton() == yes_button:
                history = self.settings_manager.load_project_history()
                history = [p for p in history if p.get("output_folder") != output_folder]
                self.settings_manager.save_project_history(history)
            return

        print("[INFO] Загрузка проекта из истории…")

        # --- НАЧАЛО НОВОЙ КЛЮЧЕВОЙ ЛОГИКИ ---
        # Проверяем, отличается ли загружаемый проект от текущего
        epub_path = self._resolve_project_epub_path(project_data)
        if not epub_path or not os.path.exists(epub_path):
            return

        if self.selected_file != epub_path or self.output_folder != output_folder:
            print("[INFO] Обнаружена смена проекта. Полный сброс состояния...")
            self.html_files = []
            self.paths_widget.update_chapters_info(0)
            if self.task_manager:
                self.task_manager.clear_all_queues()
        # --- КОНЕЦ НОВОЙ КЛЮЧЕВОЙ ЛОГИКИ ---

        self.selected_file = epub_path
        self.output_folder = output_folder
        self.paths_widget.set_file_path(epub_path)
        self.paths_widget.set_folder_path(output_folder)
        self.project_manager = TranslationProjectManager(self.output_folder)

        # Запускаем процесс выбора глав. Дальнейшее обновление UI произойдет в колбэках.
        # Теперь это безопасно, так как self.html_files гарантированно либо пуст, либо актуален.
        self._process_selected_file()



    def _calibrate_cpu(self, no_log=False):
        """
        Выполняет эталонный тест ВСЕГО конвейера фильтрации глоссария,
        учитывая текущие настройки пользователя (порог Fuzzy, Jieba).
        """
        if not no_log:
            print("[INFO] Запуск ручной калибровки CPU на реальных данных проекта…")

        current_glossary_list = self.glossary_widget.get_glossary()
        if not current_glossary_list or not self.html_files:
            QMessageBox.warning(self, "Недостаточно данных", "Для калибровки необходимо выбрать EPUB с главами и загрузить глоссарий.")
            return

        glossary_sample_list = current_glossary_list[:BENCHMARK_GLOSSARY_SIZE]
        # Для теста нам нужен полный формат словаря
        glossary_sample_dict = {}
        for entry in glossary_sample_list:
            original = str(entry.get('original') or "").strip()
            if not original:
                continue
            glossary_sample_dict[original] = {
                'rus': str(entry.get('rus') or ""),
                'note': str(entry.get('note') or "")
            }

        text_sample = ""
        if self.html_files and self.selected_file:
            try:
                with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as zf:
                    first_chapter_content = zf.read(self.html_files[0]).decode('utf-8', 'ignore')
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(first_chapter_content, 'html.parser')
                    full_text = soup.get_text()
                    start_index = max(0, (len(full_text) - BENCHMARK_TEXT_SIZE) // 2)
                    text_sample = full_text[start_index : start_index + BENCHMARK_TEXT_SIZE]
            except Exception:
                text_sample = "placeholder " * (BENCHMARK_TEXT_SIZE // 12)
        else:
            text_sample = "placeholder " * (BENCHMARK_TEXT_SIZE // 12)

        filter_instance = SmartGlossaryFilter()

        # 1. Получаем ВСЕ актуальные настройки фильтрации из UI.
        current_threshold = self.model_settings_widget.fuzzy_threshold_spin.value()
        use_jieba_for_test = self.model_settings_widget.use_jieba_glossary_checkbox.isChecked()

        start_time = time.perf_counter()

        # 2. Вызываем главный метод-оркестратор, а не его внутреннюю часть.
        #    Это гарантирует, что мы тестируем всю цепочку оптимизаций.
        settings = self.get_settings()
        self.context_manager.update_settings(settings)
        sim_map = self.context_manager.similarity_map

        filter_instance.filter_glossary_for_text(
            full_glossary=glossary_sample_dict,
            text=text_sample,
            fuzzy_threshold=current_threshold,
            use_jieba_for_glossary_search=use_jieba_for_test,
            similarity_map=sim_map
        )

        end_time = time.perf_counter()

        time_taken = end_time - start_time
        if time_taken < 0.001: time_taken = 0.001

        num_operations = len(glossary_sample_dict) * len(text_sample)
        self.cpu_performance_index = num_operations / time_taken

        # 3. Добавляем в лог все использованные параметры для полной прозрачности.
        fuzzy_mode_info = f"Fuzzy порог {current_threshold}%" if current_threshold < 100 else "Fuzzy выключен"
        if not no_log:
            print(f"[INFO] Калибровка ({fuzzy_mode_info}, Jieba: {'Вкл' if use_jieba_for_test else 'Выкл'}) завершена за {time_taken:.4f} сек. "
              f"Индекс: {self.cpu_performance_index:,.0f} (термин*сим)/сек.")

        self._update_fuzzy_status_display()
        if no_log == True:
            QtCore.QTimer.singleShot(600, lambda: self._calibrate_cpu(no_log=False))

    @QtCore.pyqtSlot()
    def _update_fuzzy_status_display(self):
        """
        ТОЛЬКО обновляет UI-лейбл на основе текущих настроек и последней калибровки.
        Версия 2.0: Корректно учитывает количество клиентов (параллельных окон).
        """
        label = self.model_settings_widget.fuzzy_status_label
        dynamic_glossary_enabled = self.model_settings_widget.dynamic_glossary_checkbox.isChecked()
        fuzzy_threshold = self.model_settings_widget.fuzzy_threshold_spin.value()

        if not dynamic_glossary_enabled:
            label.setText("Fuzzy-поиск: выключен (динамический глоссарий отключен)")
            label.setToolTip("Динамический глоссарий отключен, поэтому fuzzy-фильтрация не выполняется.")
            label.setStyleSheet("color: #aaa; font-size: 10px;")
            return

        if fuzzy_threshold >= 100:
            label.setText("Fuzzy-поиск: выключен (точный поиск)")
            label.setToolTip("Порог 100% отключает нечеткий fuzzy-поиск. Используется быстрый точный поиск по глоссарию.")
            label.setStyleSheet("color: #aaa; font-size: 10px;")
            return

        if self.cpu_performance_index is None or self.cpu_performance_index == 0:
            label.setText("Fuzzy-поиск: (требуется калибровка 🔄)")
            label.setStyleSheet("color: #aaa;")
            return

        # --- Получаем все необходимые данные ---
        glossary_size = len(self.glossary_widget.get_glossary())
        rpm = self.model_settings_widget.rpm_spin.value()

        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---
        # 1. Получаем количество параллельных клиентов из spinbox'а.
        num_clients = self.instances_spin.value()
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---

        use_batching = self.translation_options_widget.batch_checkbox.isChecked()
        use_chunking = self.translation_options_widget.chunking_checkbox.isChecked()
        avg_task_size = 0
        if use_batching or use_chunking:
            avg_task_size = self.translation_options_widget.task_size_spin.value()
        elif self.html_files:
            total_size = sum(self.translation_options_widget.chapter_compositions.get(f, {}).get('total_size', 0) for f in self.html_files)
            avg_task_size = total_size / len(self.html_files) if self.html_files else 0

        # --- Проверки и расчеты ---
        if glossary_size == 0 or rpm == 0 or avg_task_size == 0 or num_clients == 0:
            return

        num_operations = glossary_size * avg_task_size
        estimated_time = num_operations / self.cpu_performance_index

        # --- НАЧАЛО КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---
        # 2. Рассчитываем ОБЩУЮ пропускную способность и РЕАЛЬНЫЙ интервал между запросами.
        total_application_rpm = rpm * num_clients
        interval = 60 / total_application_rpm
        # --- КОНЕЦ КЛЮЧЕВОГО ИСПРАВЛЕНИЯ ---

        if estimated_time > interval:
            label.setText(f"Fuzzy-поиск: ~{estimated_time:.2f} сек. (Дольше, чем {interval:.2f}с/запрос. 🔴)")
            # Добавляем более детальную подсказку
            label.setToolTip(f"При {num_clients} клиентах общая частота запросов составляет ~{int(total_application_rpm)} RPM.\n"
                             f"Интервал между запросами от приложения: ~{interval:.2f} сек.\n"
                             f"Время поиска в глоссарии (~{estimated_time:.2f} сек.) превышает этот интервал, что грозит тотальным зависанием.")
            label.setStyleSheet("color: red; font-size: 10px; font-weight: bold;")
        else:
            label.setText(f"Fuzzy-поиск: ~{estimated_time:.2f} сек. (OK)")
            label.setToolTip(f"Время поиска в глоссарии (~{estimated_time:.2f} сек.) меньше интервала\n"
                             f"между запросами (~{interval:.2f} сек.), поэтому он не будет 'тормозить' перевод.")
            label.setStyleSheet("color: green; font-size: 10px; font-weight: bold;")

    def _process_project_folder(self, folder):
        """
        Центральный, но теперь УПРОЩЕННЫЙ метод для обработки папки проекта.
        Синхронизация и миграция теперь делегированы EpubHtmlSelectorDialog.
        """
        # Просто загружаем глоссарий проекта, если он есть.
        self._load_project_glossary(folder)


    def _open_epub_builder_standalone(self):
        """
        Открывает сборщик EPUB, используя уже выбранные файл и папку.
        """
        folder = self.output_folder

        map_file = os.path.join(folder, 'translation_map.json')
        if not os.path.exists(map_file):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Проект не найден")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setText("В выбранной папке отсутствует файл 'translation_map.json'.")
            msg_box.setInformativeText("Сборщик может работать некорректно. Продолжить?")
            yes_button = msg_box.addButton("Да, продолжить", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.setDefaultButton(no_button)
            msg_box.exec()
            if msg_box.clickedButton() == no_button:
                return

        try:
            # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Передаем project_manager ---
            dialog = TranslatedChaptersManagerDialog(
                folder,
                self,
                original_epub_path=self.selected_file,
                project_manager=self.project_manager # <--- ВОТ ОНО
            )
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось открыть менеджер EPUB: {e}")

    def _validate_translation_map(self, project_manager):
        """
        Проверяет карту, спрашивает пользователя и, если нужно, СИНХРОННО выполняет очистку.
        Возвращает True, если очистка была выполнена.
        """
        dead_entries = project_manager.validate_map_with_filesystem()
        if not dead_entries:
            return False

        num_dead = len(dead_entries)
        # --- ИСПРАВЛЕНИЕ: Создаем QMessageBox с родителем (self) для правильного стиля ---
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Синхронизация проекта")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setText(f"Обнаружено {num_dead} записей о переводах, файлы которых отсутствуют.")

        details = "\n".join([f"- {rel_path}" for _, _, rel_path in dead_entries[:5]])
        if num_dead > 5:
            details += f"\n… и еще {num_dead - 5}."

        msg_box.setInformativeText(f"Рекомендуется очистить эти 'мертвые' записи из карты проекта.\n\nПримеры:\n{details}")

        cleanup_button = msg_box.addButton("Очистить записи", QMessageBox.ButtonRole.AcceptRole)
        msg_box.addButton("Оставить как есть", QMessageBox.ButtonRole.RejectRole)

        msg_box.exec()

        if msg_box.clickedButton() == cleanup_button:
            # Выполняем запись в файл немедленно. Это гарантирует целостность данных.
            project_manager.cleanup_dead_entries(dead_entries)
            # --- ИСПРАВЛЕНИЕ: Убираем лишнее и проблемное окно "Выполнено" ---
            return True

        return False

# gemini_translator/ui/dialogs/setup.py

    def _estimate_auto_task_size_limit(self, token_limit: int):
        token_limit = int(token_limit or 0)
        if token_limit <= 0:
            return None, None

        chars_per_token = api_config.UNIFIED_INPUT_CHARS_PER_TOKEN
        profile_name = "единая токен-оценка"
        estimated_chars = int(round(token_limit * chars_per_token))
        estimated_chars = max(500, min(estimated_chars, 350000))
        return estimated_chars, profile_name

    def _get_effective_auto_short_ratio_limit(self, auto_settings: dict | None, result_data: dict | None = None):
        if not isinstance(auto_settings, dict):
            auto_settings = {}
        if not isinstance(result_data, dict):
            result_data = {}

        base_limit = float(auto_settings.get('retry_short_ratio', 0.70) or 0.70)
        if self._auto_result_uses_cjk_ratio(result_data):
            return max(base_limit, AUTO_CJK_SHORT_RATIO_LIMIT), "CJK"
        return base_limit, "alphabetic"

    def _auto_result_uses_cjk_ratio(self, result_data: dict | None) -> bool:
        if not isinstance(result_data, dict):
            return False

        if result_data.get('is_cjk_original') is True:
            return True

        for field_name in ('original_html', 'original_text', 'original_content'):
            source_text = result_data.get(field_name)
            if isinstance(source_text, str) and AUTO_CJK_CHAR_RE.search(source_text):
                return True

        return self._auto_original_chapter_has_cjk(result_data.get('internal_html_path'))

    def _auto_original_chapter_has_cjk(self, internal_path: str | None) -> bool:
        internal_path = str(internal_path or "").strip()
        epub_path = getattr(self, 'selected_file', None)
        if not internal_path or not epub_path or not os.path.exists(epub_path):
            return False

        cache = getattr(self, '_auto_cjk_original_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._auto_cjk_original_cache = cache

        cache_key = (os.path.abspath(epub_path), internal_path)
        if cache_key in cache:
            return cache[cache_key]

        has_cjk = False
        try:
            with zipfile.ZipFile(epub_path, 'r') as epub_zip:
                if internal_path in epub_zip.namelist():
                    original_html = epub_zip.read(internal_path).decode('utf-8', errors='ignore')
                    has_cjk = bool(AUTO_CJK_CHAR_RE.search(original_html))
        except Exception:
            has_cjk = False

        cache[cache_key] = has_cjk
        return has_cjk

    def _resolve_auto_model_override(self, auto_settings: dict | None = None):
        if not isinstance(auto_settings, dict):
            auto_settings = {}

        model_name = auto_settings.get('model_override')
        if not model_name:
            return None, None, None

        model_config = api_config.all_models().get(model_name)
        if not isinstance(model_config, dict):
            return None, None, f"Автомодель '{model_name}' не найдена в конфигурации."

        selected_provider = self.key_management_widget.get_selected_provider()
        model_provider = model_config.get('provider')
        if selected_provider and model_provider and model_provider != selected_provider:
            return None, None, (
                f"Автомодель '{model_name}' недоступна для сервиса "
                f"'{selected_provider}'."
            )

        return model_name, model_config, None

    def _get_active_keys_for_provider(self, provider_id: str | None):
        normalized_provider = str(provider_id or "").strip()
        if not normalized_provider:
            return []

        if not api_config.provider_requires_api_key(normalized_provider):
            placeholder = api_config.provider_placeholder_api_key(normalized_provider)
            return [placeholder] if placeholder else []

        key_widget = getattr(self, 'key_management_widget', None)
        if not key_widget:
            return []

        active_by_provider = getattr(key_widget, 'current_active_keys_by_provider', {})
        if isinstance(active_by_provider, dict):
            stored_keys = active_by_provider.get(normalized_provider)
            if isinstance(stored_keys, (list, tuple, set)):
                normalized_keys = [str(key).strip() for key in stored_keys if str(key).strip()]
                if normalized_keys:
                    return list(normalized_keys)

        try:
            if key_widget.get_selected_provider() == normalized_provider:
                return [
                    str(key).strip()
                    for key in key_widget.get_active_keys()
                    if str(key).strip()
                ]
        except Exception:
            return []

        return []

    def _resolve_auto_filter_redirect_override(self, auto_settings: dict | None = None):
        if not isinstance(auto_settings, dict):
            auto_settings = {}
        if not auto_settings.get('filter_redirect_enabled'):
            return None, None

        model_name = str(auto_settings.get('filter_redirect_model') or "").strip()
        if not model_name:
            return None, "Для redirect отфильтрованных глав не выбрана модель."

        model_config = api_config.all_models().get(model_name)
        if not isinstance(model_config, dict):
            return None, f"Модель redirect '{model_name}' не найдена в конфигурации."

        selected_provider = str(auto_settings.get('filter_redirect_provider') or "").strip()
        provider_id = selected_provider or str(model_config.get('provider') or "").strip()
        model_provider = str(model_config.get('provider') or "").strip()
        if not provider_id:
            return None, f"Не удалось определить сервис для модели redirect '{model_name}'."
        if model_provider and provider_id != model_provider:
            return None, (
                f"Модель redirect '{model_name}' относится к сервису '{model_provider}', "
                f"но в настройке выбран '{provider_id}'."
            )

        active_keys = self._get_active_keys_for_provider(provider_id)
        if not active_keys:
            provider_label = api_config.provider_display_map().get(provider_id, provider_id)
            return None, (
                f"Для redirect отфильтрованных глав нет активной сессии/ключей у сервиса "
                f"'{provider_label}'."
            )

        return {
            'provider': provider_id,
            'api_keys': active_keys,
            'model': model_name,
            'model_config': model_config,
        }, None

    def _get_effective_auto_model_settings(self, auto_settings: dict | None = None):
        settings = self.model_settings_widget.get_settings().copy()
        model_name, model_config, _ = self._resolve_auto_model_override(auto_settings)
        if model_name and model_config:
            settings['model'] = model_name
            settings['model_config'] = model_config
        self._apply_auto_thinking_override(settings, auto_settings, model_config=model_config)
        return settings

    def _resolve_auto_glossary_prompt_override(self, auto_settings: dict | None = None):
        if not isinstance(auto_settings, dict):
            auto_settings = {}

        selected_value = auto_settings.get('glossary_prompt_preset')
        if not isinstance(selected_value, str) or not selected_value.strip():
            return None, None, None

        builtin_presets = api_config.builtin_glossary_prompt_variants()
        builtin_meta = builtin_presets.get(selected_value)
        if isinstance(builtin_meta, dict):
            builtin_text = builtin_meta.get('text')
            builtin_label = builtin_meta.get('label') or selected_value
            if isinstance(builtin_text, str) and builtin_text.strip():
                return None, builtin_text, builtin_label
            return None, None, builtin_label

        return selected_value, None, selected_value

    def _apply_auto_thinking_override(
        self,
        settings: dict,
        auto_settings: dict | None = None,
        model_config: dict | None = None,
    ):
        if not isinstance(settings, dict):
            return
        if not isinstance(auto_settings, dict):
            auto_settings = {}

        thinking_override = str(auto_settings.get('thinking_mode_override') or 'inherit')
        if thinking_override == 'inherit':
            return

        effective_model_config = model_config
        if not isinstance(effective_model_config, dict):
            effective_model_config = settings.get('model_config')
        if not isinstance(effective_model_config, dict):
            model_name = settings.get('model')
            if isinstance(model_name, str) and model_name:
                effective_model_config = api_config.all_models().get(model_name)
        if not isinstance(effective_model_config, dict):
            return

        min_budget_cfg = effective_model_config.get('min_thinking_budget')
        thinking_levels = effective_model_config.get('thinkingLevel')
        has_thinking_config = (
            'thinkingLevel' in effective_model_config
            or 'min_thinking_budget' in effective_model_config
        )
        supports_thinking = has_thinking_config and min_budget_cfg is not False
        if not supports_thinking:
            settings['thinking_enabled'] = False
            settings['thinking_budget'] = None
            settings['thinking_level'] = None
            return

        if thinking_override == 'disabled':
            settings['thinking_enabled'] = False
            settings['thinking_budget'] = 0
            settings['thinking_level'] = None
            return

        if thinking_override.startswith('level:'):
            requested_level = thinking_override.split(':', 1)[1].strip().lower()
            available_levels = {
                str(level).strip().lower()
                for level in thinking_levels
            } if isinstance(thinking_levels, list) else set()
            if requested_level not in available_levels:
                return
            settings['thinking_enabled'] = True
            settings['thinking_level'] = requested_level.upper()
            settings['thinking_budget'] = None
            return

        if thinking_override.startswith('budget:'):
            if isinstance(thinking_levels, list) and thinking_levels:
                return

            raw_budget = thinking_override.split(':', 1)[1].strip().lower()
            if raw_budget == 'dynamic':
                parsed_budget = -1
            else:
                try:
                    parsed_budget = int(raw_budget)
                except (TypeError, ValueError):
                    return

            settings['thinking_enabled'] = True
            settings['thinking_budget'] = parsed_budget
            settings['thinking_level'] = None

    def _resolve_auto_translation_options(self, auto_settings: dict | None = None):
        translation_options = self.translation_options_widget.get_settings().copy()
        if not isinstance(auto_settings, dict):
            auto_settings = {}

        mode = str(auto_settings.get('translation_mode_override', 'inherit') or 'inherit')
        has_override = False
        if mode == 'batch':
            translation_options.update({
                'use_batching': True,
                'chunking': False,
                'chunk_on_error': False,
            })
            has_override = True
        elif mode == 'single':
            translation_options.update({
                'use_batching': False,
                'chunking': False,
                'chunk_on_error': False,
            })
            has_override = True
        elif mode == 'chunk':
            translation_options.update({
                'use_batching': False,
                'chunking': True,
                'chunk_on_error': True,
            })
            has_override = True
        else:
            mode = 'inherit'

        batch_token_limit = int(auto_settings.get('batch_token_limit_override', 0) or 0)
        batch_char_limit = None
        token_profile = None
        if batch_token_limit > 0:
            batch_char_limit, token_profile = self._estimate_auto_task_size_limit(batch_token_limit)
            if batch_char_limit:
                translation_options['task_size_limit'] = batch_char_limit
                has_override = True

        return translation_options, mode, has_override, batch_token_limit, batch_char_limit, token_profile

    def _build_sequential_chapter_chains(self, chapters: list, split_count: int) -> list[list]:
        chapters = list(chapters or [])
        if not chapters:
            return []
        try:
            split_count = int(split_count)
        except (TypeError, ValueError):
            split_count = 1
        split_count = max(1, min(split_count, len(chapters)))

        chains = []
        for index in range(split_count):
            start = (index * len(chapters)) // split_count
            end = ((index + 1) * len(chapters)) // split_count
            if start < end:
                chains.append(chapters[start:end])
        return chains

    def _prepare_and_display_tasks(self, clean_rebuild=False, translation_options_override: dict | None = None):
        """
        Собирает задачи, создает/обновляет ChapterQueueManager и
        отправляет "пульс" для перерисовки UI.
        Версия 6.0: Правильная гибридная логика.
        - clean_rebuild=True: Строит задачи с нуля из self.html_files.
        - clean_rebuild=False: Пересобирает задачи на основе текущего порядка в TaskManager.
        """
        if not self.task_manager: return

        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Выбор источника глав ---
        if clean_rebuild:
            # "Режим Архитектора": источник - исходный список.
            source_chapters = self.html_files
        else:
            # "Режим Реорганизации": источник - текущее состояние TaskManager'а.
            source_chapters = self._unpack_tasks_to_chapters()

        if not source_chapters or not self.selected_file:
            QtCore.QTimer.singleShot(10, lambda: self.task_manager.set_pending_tasks([]))
        else:
            from ...utils.glossary_tools import TaskPreparer
            import zipfile
            import os

            cached_sizes = get_epub_chapter_sizes_with_cache(self.project_manager, self.selected_file)
            real_chapter_sizes = {
                chapter: int(cached_sizes.get(chapter, 0) or 0)
                for chapter in set(source_chapters)
            }
            missing_size_chapters = [chapter for chapter, size in real_chapter_sizes.items() if size <= 0]

            if missing_size_chapters:
                try:
                    with open(self.selected_file, 'rb') as epub_file, zipfile.ZipFile(epub_file, 'r') as zf:
                        for chapter in missing_size_chapters:
                            real_chapter_sizes[chapter] = len(zf.read(chapter).decode('utf-8', 'ignore'))
                except Exception as e:
                    QtWidgets.QMessageBox.critical(
                        self,
                        "Ошибка обработки EPUB",
                        f"Не удалось прочитать EPUB для расчёта размеров глав.\n\n{e}"
                    )
                    return

            settings = self.get_settings()
            if isinstance(translation_options_override, dict):
                settings.update(translation_options_override)
            elif self._auto_workflow_enabled_for_session:
                auto_translation_settings = settings.get('auto_translation', {})
                effective_options, mode, has_override, *_ = self._resolve_auto_translation_options(auto_translation_settings)
                if has_override:
                    settings.update(effective_options)
            display_tasks_settings = settings.copy()

            preparer = TaskPreparer(display_tasks_settings, real_chapter_sizes)
            if display_tasks_settings.get('sequential_translation'):
                chapter_chains = self._build_sequential_chapter_chains(
                    source_chapters,
                    display_tasks_settings.get('sequential_translation_splits', 1),
                )
                task_chains = [
                    preparer.prepare_tasks(chapter_chain)
                    for chapter_chain in chapter_chains
                    if chapter_chain
                ]
                self.task_manager.set_pending_task_chains(task_chains)
            else:
                plain_payloads = preparer.prepare_tasks(source_chapters)
                self.task_manager.set_pending_tasks(plain_payloads)

        QtCore.QTimer.singleShot(15, lambda: self.translation_options_widget._update_info_text())


        if self.cpu_performance_index is None and self.html_files and self.glossary_widget.get_glossary():
            print("[INFO] Условия для калибровки выполнены. Запуск будет отложен…")
            self.cpu_performance_index = 1
            QtCore.QTimer.singleShot(20, lambda: self._calibrate_cpu(no_log=True))




    def _load_project_glossary(self, folder_path):
        self.glossary_widget.set_project_path(folder_path)
        project_glossary_path = os.path.join(folder_path, "project_glossary.json")
        try:
            saved_snapshot, restored_from_autosave = self.glossary_widget.load_project_glossary()
            if os.path.exists(project_glossary_path):
                print(f"[ИНФО] Глоссарий проекта загружен из: {project_glossary_path}")
            elif restored_from_autosave:
                print(f"[ИНФО] Глоссарий проекта восстановлен из автокопии: {folder_path}")
        except Exception as e:
            QMessageBox.warning(self, "Ошибка загрузки", f"Не удалось загрузить project_glossary.json: {e}")
            self.glossary_widget.clear()
            saved_snapshot = []
            restored_from_autosave = False

        self.initial_glossary_state = [item.copy() for item in saved_snapshot]
        self.is_glossary_dirty = self.glossary_widget.get_glossary() != self.initial_glossary_state
        if not self.is_glossary_dirty:
            self.glossary_widget.mark_current_state_as_saved()
        self._refresh_dirty_window_title()

    def open_translation_validator(self):
        """Открывает инструмент проверки качества переводов."""

        # Проверяем, есть ли папка для перевода (она нужна валидатору)
        if not self.output_folder or not os.path.isdir(self.output_folder):
            QMessageBox.warning(self, "Папка не выбрана", "Для запуска проверки необходимо выбрать папку проекта.")
            return

        # Проверяем, есть ли исходный EPUB (он тоже нужен)
        if not self.selected_file or not os.path.exists(self.selected_file):
            QMessageBox.warning(self, "Файл не выбран", "Для сравнения переводов необходимо выбрать исходный EPUB-файл.")
            return

        self._post_event('log_message', {'message': "[INFO] Открытие инструмента проверки переводов…"})


        self.setEnabled(False)
        self.is_blocked_by_child_dialog = True
        # Импортируем диалог прямо здесь, чтобы избежать циклических зависимостей
        from .validation import TranslationValidatorDialog
        self.validator_dialog = TranslationValidatorDialog(self.output_folder, self.selected_file, self, project_manager=self.project_manager)


        self.validator_dialog.exec()

        self.setEnabled(True)
        self.is_blocked_by_child_dialog = False
        self._check_and_sync_active_session()

        self._post_event('log_message', {'message': "[INFO] Инструмент проверки переводов закрыт."})

    def open_ai_glossary_generation(self):
        """Открывает существующий AI-генератор глоссария с выбранным шаблоном."""
        if not all([self.selected_file, self.output_folder, self.html_files]):
            QMessageBox.warning(self, "Недостаточно данных", "Сначала выберите EPUB, папку проекта и главы.")
            return

        auto_settings = self.auto_translate_widget.get_settings()
        glossary_preset_name, glossary_prompt_override, glossary_prompt_label = self._resolve_auto_glossary_prompt_override(auto_settings)
        if glossary_preset_name:
            self.settings_manager.save_last_glossary_prompt_preset_name(glossary_preset_name)
        elif glossary_prompt_override:
            self.settings_manager.save_last_glossary_prompt_preset_name(None)
            self.settings_manager.save_last_glossary_prompt_text(glossary_prompt_override)
            self._auto_log(f"Для AI-глоссария выбран шаблон: {glossary_prompt_label}.", force=True)

        self.glossary_widget.set_epub_path(self.selected_file)
        self.glossary_widget._open_ai_generation_dialog()

    def _start_auto_glossary_then_translation(self, settings: dict, auto_settings: dict):
        if self._auto_glossary_running:
            return

        from .glossary_dialogs.ai_generation import GenerationSessionDialog

        glossary_preset_name, glossary_prompt_override, glossary_prompt_label = self._resolve_auto_glossary_prompt_override(auto_settings)
        if glossary_preset_name:
            self.settings_manager.save_last_glossary_prompt_preset_name(glossary_preset_name)
        elif glossary_prompt_override:
            self.settings_manager.save_last_glossary_prompt_preset_name(None)
            self.settings_manager.save_last_glossary_prompt_text(glossary_prompt_override)
            self._auto_log(f"Автоглоссарий использует шаблон: {glossary_prompt_label}.", force=True)

        glossary_initial_settings = dict(settings)
        glossary_initial_settings['use_batching'] = True
        glossary_initial_settings['chunking'] = False
        glossary_initial_settings['chunk_on_error'] = False
        if glossary_prompt_override:
            glossary_initial_settings['glossary_generation_prompt'] = glossary_prompt_override

        glossary_model_name, glossary_model_config, _ = self._resolve_auto_model_override(auto_settings)
        if glossary_model_name and glossary_model_config:
            glossary_initial_settings['model'] = glossary_model_name
            glossary_initial_settings['model_config'] = glossary_model_config
        self._apply_auto_thinking_override(
            glossary_initial_settings,
            auto_settings,
            model_config=glossary_initial_settings.get('model_config'),
        )

        dialog = GenerationSessionDialog(
            settings_manager=self.settings_manager,
            initial_glossary=self.glossary_widget.get_glossary(),
            merge_mode=None,
            html_files=self.html_files,
            epub_path=self.selected_file,
            project_manager=self.project_manager,
            initial_ui_settings=glossary_initial_settings,
            parent=self,
            restore_saved_ui_settings=False,
            persist_ui_settings=False,
        )
        dialog.hide()
        dialog.generation_finished.connect(self._on_auto_glossary_generation_finished)
        dialog.finished.connect(self._on_auto_glossary_dialog_closed)

        self._auto_glossary_dialog = dialog
        self._auto_glossary_running = True
        self._auto_glossary_pending_translation = True
        self._auto_glossary_completed = False
        self._auto_followup_running = True
        self.is_blocked_by_child_dialog = True
        self._set_controls_enabled(False)
        self.start_btn.setEnabled(False)
        self._auto_log("Запускаю автосоставление глоссария перед переводом…", force=True)

        dialog._initial_load_done = True
        dialog._deferred_initial_load()
        dialog._start_session()
        self._auto_glossary_poll_timer.start()

    def _poll_auto_glossary_dialog(self):
        dialog = self._auto_glossary_dialog
        if not dialog:
            self._auto_glossary_poll_timer.stop()
            return

        if dialog.is_session_active:
            return

        if getattr(dialog, '_session_finished_successfully', False):
            try:
                dialog._refresh_glossary_from_db()
                dialog._update_start_button_state()
            except Exception as e:
                self._auto_log(f"Не удалось подготовить результаты автоглоссария к применению: {e}", force=True)
            self._auto_glossary_poll_timer.stop()
            dialog.accept()
            return

        self._auto_glossary_poll_timer.stop()
        self._auto_log("Автоглоссарий завершился без успешного финиша. Основной перевод не будет запущен.", force=True)
        try:
            dialog._cleanup(keep_recovery_file=True)
        finally:
            QtWidgets.QDialog.reject(dialog)

    @pyqtSlot(list, set)
    def _on_auto_glossary_generation_finished(self, final_glossary: list, processed_chapters: set):
        self._auto_glossary_completed = True

        normalized_glossary = []
        if isinstance(final_glossary, list):
            normalized_glossary = [item.copy() for item in final_glossary if isinstance(item, dict)]

        if not normalized_glossary and self._auto_glossary_dialog and hasattr(self._auto_glossary_dialog, 'glossary_widget'):
            try:
                normalized_glossary = [
                    item.copy()
                    for item in self._auto_glossary_dialog.glossary_widget.get_glossary()
                    if isinstance(item, dict)
                ]
            except Exception as e:
                self._auto_log(f"Не удалось прочитать финальный глоссарий из скрытого диалога: {e}", force=True)

        if normalized_glossary:
            self.glossary_widget.set_glossary(normalized_glossary)
        else:
            self._auto_log("Автоглоссарий завершился без пригодного списка терминов для основного окна.", force=True)

        if self.output_folder:
            try:
                project_glossary_path = os.path.join(self.output_folder, "project_glossary.json")
                with open(project_glossary_path, 'w', encoding='utf-8') as f:
                    json.dump(self.glossary_widget.get_glossary(), f, ensure_ascii=False, indent=2, sort_keys=True)
            except Exception as e:
                self._auto_log(f"Не удалось сохранить автоглоссарий в проект: {e}", force=True)

        if self.project_manager and processed_chapters is not None:
            try:
                self.project_manager.save_glossary_generation_map(set(processed_chapters))
            except Exception as e:
                self._auto_log(f"Не удалось сохранить карту автоглоссария: {e}", force=True)

        self.mark_project_glossary_as_saved(self.glossary_widget.get_glossary())
        self._prepare_and_display_tasks(clean_rebuild=True)
        self._auto_log(
            f"Автоглоссарий завершён: терминов {len(self.glossary_widget.get_glossary())}. Запускаю основной перевод…",
            force=True
        )

        self._auto_glossary_pending_translation = False
        QtCore.QTimer.singleShot(
            250,
            lambda: self._start_translation(
                is_auto_restart=False,
                skip_auto_glossary=True,
                preserve_log=True,
            )
        )

    @pyqtSlot(int)
    def _on_auto_glossary_dialog_closed(self, result: int):
        self._auto_glossary_poll_timer.stop()
        self._auto_glossary_dialog = None
        self._auto_glossary_running = False
        self._auto_followup_running = False
        self.is_blocked_by_child_dialog = False

        if self._auto_glossary_pending_translation and not self._auto_glossary_completed:
            self._auto_glossary_pending_translation = False
            self._auto_log("Автоглоссарий прерван. Основной перевод не был запущен.", force=True)

        self._auto_glossary_completed = False
        if not self.is_session_active:
            self._set_controls_enabled(True)
            self.check_ready()

    def open_ai_consistency_checker(self):
        """Открывает существующий диалог AI-проверки согласованности."""
        if not self.project_manager or not self.settings_manager:
            QMessageBox.warning(self, "Нет проекта", "Сначала загрузите проект перевода.")
            return

        chapters_to_analyze = load_project_chapters_for_consistency(self.project_manager)
        if not chapters_to_analyze:
            QMessageBox.warning(self, "Нет данных", "Не найдено переведённых глав для AI-проверки согласованности.")
            return

        from .consistency_checker import ConsistencyValidatorDialog

        dialog = ConsistencyValidatorDialog(
            chapters_to_analyze,
            self.settings_manager,
            self,
            project_manager=self.project_manager
        )
        if hasattr(dialog, '_update_chunk_stats'):
            dialog._update_chunk_stats()
        dialog.exec()

    def _auto_log(
        self,
        message: str,
        force: bool = False,
        details_text: str | None = None,
        details_title: str | None = None,
        file_path: str | None = None,
        file_label: str | None = None,
    ):
        auto_settings = self.auto_translate_widget.get_settings() if hasattr(self, 'auto_translate_widget') else {}
        if force or auto_settings.get('log_each_step', True):
            payload = {'message': f"[AUTO] {message}"}
            if isinstance(details_text, str) and details_text.strip():
                payload['details_text'] = details_text
                if isinstance(details_title, str) and details_title.strip():
                    payload['details_title'] = details_title
            if isinstance(file_path, str) and file_path.strip():
                payload['file_path'] = file_path
                if isinstance(file_label, str) and file_label.strip():
                    payload['file_label'] = file_label
            self._post_event('log_message', payload)

    def _extract_chapters_from_payload(self, payload) -> list[str]:
        if not payload:
            return []

        task_type = payload[0]
        if task_type in ('epub', 'epub_chunk') and len(payload) > 2:
            return [payload[2]]
        if task_type == 'epub_batch' and len(payload) > 2:
            return list(payload[2])
        return []

    def _normalize_auto_chapters(self, chapters, preserve_order: bool = False) -> list[str]:
        if not chapters:
            return []

        normalized = []
        seen = set()
        for chapter in chapters:
            if not isinstance(chapter, str) or not chapter:
                continue
            if chapter in seen:
                continue
            seen.add(chapter)
            normalized.append(chapter)

        if preserve_order:
            return normalized
        return sorted(normalized, key=extract_number_from_path)

    def _make_auto_chapter_signature(self, chapters) -> tuple[str, ...]:
        return tuple(self._normalize_auto_chapters(chapters, preserve_order=False))

    def _short_auto_name(self, chapter: str, max_length: int = 84) -> str:
        text = os.path.basename(chapter) if isinstance(chapter, str) else str(chapter)
        if len(text) <= max_length:
            return text
        return text[:max_length - 1] + "…"

    def _format_auto_chapter_list(self, chapters, limit: int = 8, preserve_order: bool = False) -> str:
        normalized = self._normalize_auto_chapters(chapters, preserve_order=preserve_order)
        if not normalized:
            return "нет глав"

        display_items = [self._short_auto_name(chapter) for chapter in normalized[:limit]]
        if len(normalized) > limit:
            display_items.append(f"… +{len(normalized) - limit}")
        return ", ".join(display_items)

    def _compose_auto_details(self, sections) -> str:
        blocks = []
        for title, content in sections:
            text = ""
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, (list, tuple)):
                lines = [str(item).strip() for item in content if str(item).strip()]
                text = "\n".join(f"- {line}" for line in lines)
            elif isinstance(content, set):
                lines = [
                    str(item).strip()
                    for item in sorted(content, key=extract_number_from_path)
                    if str(item).strip()
                ]
                text = "\n".join(f"- {line}" for line in lines)
            if not text:
                continue
            if title:
                blocks.append(f"{title}:\n{text}")
            else:
                blocks.append(text)
        return "\n\n".join(blocks)

    @staticmethod
    def _truncate_auto_trace_text(text: str | None, limit: int = 4000) -> str:
        normalized = str(text or "").strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(0, limit - 16)].rstrip() + "\n...[truncated]..."

    @staticmethod
    def _merge_auto_details(*parts: str) -> str:
        return "\n\n".join(
            str(part).strip()
            for part in parts
            if isinstance(part, str) and part.strip()
        )

    def _compose_auto_trace_details(self, traces, max_entries: int = 4, text_limit: int = 4000) -> str:
        phase_titles = {
            'glossary_collection': "Glossary collection",
            'analysis': "Analysis",
            'fix': "Fix",
        }
        trace_items = [trace for trace in (traces or []) if isinstance(trace, dict)]
        total = len(trace_items)
        if not total:
            return ""

        blocks = []
        for index, trace in enumerate(trace_items[:max_entries], start=1):
            phase_name = phase_titles.get(trace.get('phase'), str(trace.get('phase') or "trace"))
            header = f"[{index}/{total}] {phase_name}"
            chapter_names = [
                str(name).strip()
                for name in (trace.get('chapter_names') or [])
                if str(name).strip()
            ]
            if chapter_names:
                header += f" · {self._format_auto_chapter_list(chapter_names, limit=4, preserve_order=True)}"

            metadata = trace.get('metadata') if isinstance(trace.get('metadata'), dict) else {}
            metadata_lines = []
            for key in ('chunk_index', 'total_chunks', 'mode', 'problem_count', 'batch_mode'):
                value = metadata.get(key)
                if value is None:
                    continue
                metadata_lines.append(f"{key}: {value}")

            entry_parts = [header]
            if metadata_lines:
                entry_parts.append("Метаданные:\n" + "\n".join(f"- {line}" for line in metadata_lines))

            prompt_text = self._truncate_auto_trace_text(trace.get('prompt'), text_limit)
            if prompt_text:
                entry_parts.append(f"Запрос:\n{prompt_text}")

            response_text = self._truncate_auto_trace_text(trace.get('response'), text_limit)
            if response_text:
                entry_parts.append(f"Ответ:\n{response_text}")

            blocks.append("\n\n".join(entry_parts))

        if total > max_entries:
            blocks.append(f"... скрыто трассировок: {total - max_entries}")

        return "\n\n".join(blocks)

    def _describe_auto_payload(self, payload) -> str:
        chapters = self._extract_chapters_from_payload(payload)
        task_type = payload[0] if payload else "unknown"

        if task_type == 'epub_batch':
            return (
                f"пакет {len(chapters)} глав: "
                f"{self._format_auto_chapter_list(chapters, limit=5, preserve_order=True)}"
            )
        if task_type == 'epub_chunk':
            return (
                f"чанк: "
                f"{self._format_auto_chapter_list(chapters, limit=3, preserve_order=True)}"
            )
        if task_type == 'epub':
            return (
                f"глава: "
                f"{self._format_auto_chapter_list(chapters, limit=1, preserve_order=True)}"
            )
        return (
            f"{task_type}: "
            f"{self._format_auto_chapter_list(chapters, limit=4, preserve_order=True)}"
        )

    def _log_auto_payload_plan(self, title: str, payloads, max_payloads: int = 6):
        if not payloads:
            return

        total = len(payloads)
        details_lines = [
            f"[{index}/{total}] {self._describe_auto_payload(payload)}"
            for index, payload in enumerate(payloads[:max_payloads], start=1)
        ]
        if total > max_payloads:
            details_lines.append(f"… не показано ещё {total - max_payloads} пакетов.")
        self._auto_log(
            f"{title}: подготовлено {total} пакетов.",
            details_title=f"[AUTO] {title}",
            details_text="\n".join(details_lines),
        )

    def _collect_failed_chapters_by_errors(self, error_types: set[str]) -> set[str]:
        if not (self.engine and self.engine.task_manager and error_types):
            return set()

        chapters_to_retry = set()
        for task_info, status, details in self.engine.task_manager.get_ui_state_list():
            if status != 'error':
                continue

            error_map = details.get('errors', {}) if isinstance(details, dict) else {}
            if not any(error_name in error_map for error_name in error_types):
                continue

            chapters_to_retry.update(self._extract_chapters_from_payload(task_info[1]))

        return chapters_to_retry

    def _reset_auto_workflow_state(self):
        self._auto_workflow_enabled_for_session = False
        self._auto_workflow_round = 0
        self._auto_followup_running = False
        self._auto_last_retry_signatures = set()
        self._auto_last_untranslated_fix_signatures = set()
        self._auto_pending_network_retry_chapters = set()
        self._auto_filter_repack_signatures = set()
        self._auto_filter_redirect_signatures = set()
        self._auto_restart_session_override = None
        self._auto_validator_dialog = None
        self._auto_consistency_worker = None

    def _schedule_auto_workflow_followup(self, reason: str):
        if reason != "Сессия успешно завершена":
            if self._auto_workflow_enabled_for_session:
                self._auto_log(f"Автопайплайн остановлен: '{reason}'.", force=True)
            self._reset_auto_workflow_state()
            return

        if not self._auto_workflow_enabled_for_session:
            return

        QtCore.QTimer.singleShot(250, self._run_auto_workflow_followup)

    def _run_auto_workflow_followup(self):
        if self.is_session_active or self._auto_followup_running:
            return

        auto_settings = self.auto_translate_widget.get_settings()
        if not auto_settings.get('enabled'):
            self._reset_auto_workflow_state()
            return

        max_rounds = int(auto_settings.get('max_rounds', 3))
        if self._auto_workflow_round >= max_rounds:
            self._auto_log(f"Достигнут лимит автоциклов ({max_rounds}). Дальше только вручную.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
            return

        network_retry_chapters = set()
        if auto_settings.get('retry_network_failed_enabled'):
            network_retry_chapters.update(self._auto_pending_network_retry_chapters)
            network_retry_chapters.update(self._collect_failed_chapters_by_errors({'NETWORK'}))
        else:
            self._auto_pending_network_retry_chapters = set()

        if auto_settings.get('filter_repack_enabled') and self._try_auto_filter_recovery(
            auto_settings,
            deferred_retry_chapters=network_retry_chapters,
        ):
            return

        if auto_settings.get('filter_redirect_enabled') and self._try_auto_filter_redirect_followup(
            auto_settings,
            deferred_retry_chapters=network_retry_chapters,
        ):
            return

        if network_retry_chapters:
            self._run_auto_network_retry_followup(auto_settings, network_retry_chapters)
            return

        if auto_settings.get('retry_short_enabled') or auto_settings.get('retry_untranslated_enabled'):
            self._run_auto_validator_followup(auto_settings)
            return

        if auto_settings.get('ai_consistency_enabled'):
            self._run_auto_consistency_followup(auto_settings)
            return

        self._auto_log("Автопайплайн завершён без дополнительных действий.", force=True)
        self._reset_auto_workflow_state()
        self.check_ready()

    def _try_auto_filter_recovery(self, auto_settings: dict, deferred_retry_chapters=None) -> bool:
        if not (self.engine and self.engine.task_manager and self.project_manager):
            return False

        all_tasks_state = self.engine.task_manager.get_ui_state_list()
        filtered_chapters = set()
        successful_chapters = set()
        successful_map = {}
        deferred_retry_chapters = set(deferred_retry_chapters or [])

        for original, versions in self.project_manager.get_full_map().items():
            for suffix, rel_path in versions.items():
                if suffix != 'filtered':
                    full_path = os.path.join(self.project_manager.project_folder, rel_path)
                    if os.path.exists(full_path):
                        successful_map[original] = full_path
                        break

        for task_info, status, details in all_tasks_state:
            payload = task_info[1]
            chapters_in_task = self._extract_chapters_from_payload(payload)

            is_filtered = (status == 'error' and 'CONTENT_FILTER' in details.get('errors', {}))
            for chapter in chapters_in_task:
                if is_filtered:
                    filtered_chapters.add(chapter)
                elif status == 'success' and chapter in successful_map:
                    successful_chapters.add(chapter)

        if not filtered_chapters:
            return False

        filter_signature = self._make_auto_chapter_signature(filtered_chapters)
        if auto_settings.get('filter_redirect_enabled') and self._auto_filter_repack_signatures:
            return False

        self._auto_log(
            f"Content filter найден в {len(filtered_chapters)} главах: "
            f"{self._format_auto_chapter_list(filtered_chapters, limit=10)}",
            force=True,
            details_title="[AUTO] Content filter: главы",
            details_text=self._compose_auto_details([
                ("Главы с content filter", self._normalize_auto_chapters(filtered_chapters)),
            ]),
        )

        real_chapter_sizes = {
            path: composition.get('total_size', 0)
            for path, composition in self.translation_options_widget.chapter_compositions.items()
        }
        if not real_chapter_sizes:
            self._auto_log("Не удалось получить размеры глав для автопереупаковки фильтра.", force=True)
            return False

        dialog = FilterPackagingDialog(
            filtered_chapters=list(filtered_chapters),
            successful_chapters=list(successful_chapters),
            recommended_size=self.translation_options_widget.task_size_spin.value(),
            epub_path=self.selected_file,
            real_chapter_sizes=real_chapter_sizes,
            parent=self
        )
        dialog.chapters_per_batch_spin.setValue(int(auto_settings.get('filter_repack_batch_size', 3)))
        dialog.dilute_checkbox.setChecked(bool(auto_settings.get('filter_repack_dilute', True)))
        result = dialog._calculate_new_chapter_list()
        if not result:
            return False

        self._auto_filter_repack_signatures.add(filter_signature)
        deferred_retry_chapters.difference_update(filtered_chapters)
        if deferred_retry_chapters:
            self._auto_pending_network_retry_chapters.update(deferred_retry_chapters)
            self._auto_log(
                f"Сетевые повторы ({len(deferred_retry_chapters)} глав) отложены до завершения цикла обхода фильтра.",
                force=True,
                details_title="[AUTO] Отложенные сетевые главы",
                details_text=self._compose_auto_details([
                    ("Главы", self._normalize_auto_chapters(deferred_retry_chapters)),
                ]),
            )

        self._process_filter_dialog_result(result)
        self._auto_log(f"Подготовлены новые пакеты для обхода фильтра ({len(filtered_chapters)} глав).", force=True)
        result_type = result.get('type')
        if result_type == 'payloads':
            self._log_auto_payload_plan("План обхода фильтра", result.get('data', []))
        elif result_type == 'chapters':
            self._auto_log(
                "Главы для обхода фильтра: "
                f"{self._format_auto_chapter_list(result.get('data', []), limit=10)}",
                details_title="[AUTO] Главы для обхода фильтра",
                details_text=self._compose_auto_details([
                    ("Главы", self._normalize_auto_chapters(result.get('data', []), preserve_order=True)),
                ]),
            )

        self._auto_restart_session_override = None
        if auto_settings.get('auto_restart_after_retry', True):
            self._auto_workflow_round += 1
            self._auto_followup_running = True
            self.start_btn.setEnabled(False)
            QtCore.QTimer.singleShot(250, lambda: self._start_translation(is_auto_restart=True))
        else:
            self._auto_restart_session_override = None
            self._auto_log("Пакеты собраны, но автоперезапуск отключён. Можно запускать вручную.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
        return True

    def _try_auto_filter_redirect_followup(self, auto_settings: dict, deferred_retry_chapters=None) -> bool:
        if not (self.engine and self.engine.task_manager):
            return False

        all_tasks_state = self.engine.task_manager.get_ui_state_list()
        filtered_chapters = set()
        deferred_retry_chapters = set(deferred_retry_chapters or [])

        for task_info, status, details in all_tasks_state:
            payload = task_info[1]
            chapters_in_task = self._extract_chapters_from_payload(payload)
            is_filtered = (status == 'error' and 'CONTENT_FILTER' in details.get('errors', {}))
            if not is_filtered:
                continue
            for chapter in chapters_in_task:
                filtered_chapters.add(chapter)

        if not filtered_chapters:
            return False

        filter_signature = self._make_auto_chapter_signature(filtered_chapters)
        if auto_settings.get('filter_repack_enabled') and not self._auto_filter_repack_signatures:
            return False
        if self._auto_filter_redirect_signatures:
            return False

        redirect_override, redirect_warning = self._resolve_auto_filter_redirect_override(auto_settings)
        if not redirect_override:
            if redirect_warning:
                self._auto_log(
                    f"{redirect_warning} Redirect пропущен.",
                    force=True,
                )
            return False

        normalized_chapters = self._normalize_auto_chapters(filtered_chapters, preserve_order=False)
        self._auto_filter_redirect_signatures.add(filter_signature)
        self._auto_restart_session_override = redirect_override

        deferred_retry_chapters.difference_update(filtered_chapters)
        if deferred_retry_chapters:
            self._auto_pending_network_retry_chapters.update(deferred_retry_chapters)
            self._auto_log(
                f"Сетевые повторы ({len(deferred_retry_chapters)} глав) отложены до завершения redirect после фильтра.",
                force=True,
                details_title="[AUTO] Отложенные сетевые главы",
                details_text=self._compose_auto_details([
                    ("Главы", self._normalize_auto_chapters(deferred_retry_chapters)),
                ]),
            )

        self.html_files = normalized_chapters
        self.paths_widget.update_chapters_info(len(self.html_files))
        self._prepare_and_display_tasks(
            clean_rebuild=True,
            translation_options_override=self._get_filter_retry_translation_options(),
        )
        self.task_management_widget.set_retry_filtered_button_visible(False)

        redirect_provider = redirect_override.get('provider')
        redirect_provider_label = api_config.provider_display_map().get(
            redirect_provider,
            redirect_provider,
        )
        self._auto_log(
            "Главы с пометкой 'Фильтр' перенаправлены "
            f"в {redirect_provider_label}: {redirect_override.get('model')}.",
            force=True,
            details_title="[AUTO] Redirect после filter repack",
            details_text=self._compose_auto_details([
                ("Главы", normalized_chapters),
            ]),
        )

        if auto_settings.get('auto_restart_after_retry', True):
            self._auto_workflow_round += 1
            self._auto_followup_running = True
            self.start_btn.setEnabled(False)
            QtCore.QTimer.singleShot(250, lambda: self._start_translation(is_auto_restart=True))
        else:
            self._auto_restart_session_override = None
            self._auto_log("Redirect подготовлен, но автоперезапуск отключён. Можно запускать вручную.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
        return True

    def _run_auto_network_retry_followup(self, auto_settings: dict, chapters_to_retry):
        chapters = tuple(sorted(set(chapters_to_retry), key=extract_number_from_path))
        self._auto_pending_network_retry_chapters = set()
        if not chapters:
            return

        signature = ('__network__',) + chapters
        if signature in self._auto_last_retry_signatures:
            self._auto_log(
                "Получен тот же набор сетевых ошибок. Автоцикл остановлен: "
                f"{self._format_auto_chapter_list(chapters, limit=10)}.",
                force=True
            )
            self._reset_auto_workflow_state()
            self.check_ready()
            return

        self._auto_last_retry_signatures.add(signature)
        self.add_files_for_retry(self.selected_file, list(chapters))
        self._auto_log(
            f"Сетевые сбои: возвращаю в очередь {len(chapters)} глав для повторного запуска.",
            force=True,
            details_title="[AUTO] Сетевой retry",
            details_text=self._compose_auto_details([
                ("Главы", list(chapters)),
            ]),
        )

        if auto_settings.get('auto_restart_after_retry', True):
            delay_seconds = int(auto_settings.get('retry_network_failed_delay_sec', 60))
            self._auto_workflow_round += 1
            self._auto_followup_running = True
            self.start_btn.setEnabled(False)
            self._auto_log(f"Ожидаю {delay_seconds} сек. перед повторным запуском сетевых задач.", force=True)
            QtCore.QTimer.singleShot(delay_seconds * 1000, lambda: self._start_translation(is_auto_restart=True))
        else:
            self._auto_log("Сетевые задачи подготовлены к повтору, но автоперезапуск выключен.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()

    def _run_auto_validator_followup(self, auto_settings: dict):
        if not self.output_folder or not self.selected_file:
            self._auto_log("Автовалидатор пропущен: не найден проект.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
            return

        from .validation import TranslationValidatorDialog

        self._auto_followup_running = True
        self.start_btn.setEnabled(False)
        self._auto_log("Запускаю скрытую автопроверку перевода…", force=True)

        dialog = TranslationValidatorDialog(
            self.output_folder,
            self.selected_file,
            self,
            retry_enabled=False,
            project_manager=self.project_manager
        )
        dialog.hide()
        self._auto_validator_dialog = dialog

        wait_loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(250, wait_loop.quit)
        wait_loop.exec()

        dialog.check_show_all.setChecked(True)
        dialog.check_revalidate_ok.setChecked(True)
        if not dialog.path_row_map:
            self._auto_followup_running = False
            self._auto_validator_dialog = None
            dialog.deleteLater()
            self._finish_auto_validator_followup(auto_settings)
            return

        dialog.start_analysis()
        if dialog.analysis_thread:
            dialog.analysis_thread.analysis_finished.connect(self._on_auto_validator_finished)
        else:
            self._auto_followup_running = False
            self._auto_validator_dialog = None
            dialog.deleteLater()
            self._reset_auto_workflow_state()
            self.check_ready()

    def _finish_auto_validator_followup(self, auto_settings: dict, log_message: str | None = None):
        if log_message:
            self._auto_log(log_message, force=True)

        if auto_settings.get('ai_consistency_enabled'):
            self._run_auto_consistency_followup(auto_settings)
            return

        self._reset_auto_workflow_state()
        self.check_ready()

    def _on_auto_validator_finished(self, total_scanned: int, suspicious_found: int):
        dialog = self._auto_validator_dialog
        auto_settings = self.auto_translate_widget.get_settings()
        retry_short_enabled = bool(auto_settings.get('retry_short_enabled'))
        retry_untranslated_enabled = bool(auto_settings.get('retry_untranslated_enabled'))
        chapters_to_retry = set()
        chapters_to_fix_untranslated = set()
        ratio_profiles = {}
        auto_fix_result = None
        undertranslation_request_details = ""
        if dialog:
            for data in dialog.results_data.values():
                if not isinstance(data, dict):
                    continue
                internal_path = data.get('internal_html_path')
                if not internal_path:
                    continue

                ratio_value = data.get('ratio_value')
                effective_ratio_limit, ratio_profile = self._get_effective_auto_short_ratio_limit(auto_settings, data)
                needs_short_retry = (
                    retry_short_enabled
                    and isinstance(ratio_value, (int, float))
                    and data.get('len_orig', 0) > 100
                    and ratio_value < effective_ratio_limit
                )
                needs_untranslated_fix = (
                    retry_untranslated_enabled
                    and bool(data.get('untranslated_words'))
                )

                if needs_short_retry:
                    chapters_to_retry.add(internal_path)
                    data['auto_retry_ratio_limit'] = effective_ratio_limit
                    data['auto_retry_ratio_profile'] = ratio_profile
                    ratio_profiles[internal_path] = (
                        ratio_value,
                        effective_ratio_limit,
                        ratio_profile,
                    )
                elif needs_untranslated_fix:
                    chapters_to_fix_untranslated.add(internal_path)

            if chapters_to_fix_untranslated:
                fix_signature = tuple(sorted(chapters_to_fix_untranslated))
                if hasattr(dialog, 'build_auto_untranslated_request_details'):
                    try:
                        undertranslation_request_details = dialog.build_auto_untranslated_request_details(
                            target_internal_paths=fix_signature,
                            batch_size=50,
                        ) or ""
                    except Exception:
                        undertranslation_request_details = ""
                self._auto_log(
                    f"Недоперевод найден в {len(fix_signature)} главах: "
                    f"{self._format_auto_chapter_list(fix_signature, limit=10)}",
                    force=True,
                    details_title="[AUTO] Недоперевод: детали",
                    details_text=undertranslation_request_details or None,
                )
                if fix_signature in self._auto_last_untranslated_fix_signatures:
                    self._auto_log(
                        "Получен тот же набор глав с недопереводом после точечного исправления. "
                        f"Повторный точечный фикс пропущен: {self._format_auto_chapter_list(fix_signature, limit=10)}.",
                        force=True
                    )
                    dialog.deleteLater()
                    self._auto_validator_dialog = None
                    self._auto_followup_running = False
                    self._finish_auto_validator_followup(
                        auto_settings,
                        "Продолжаю автопайплайн без повторного точечного фикса недоперевода.",
                    )
                    return

                self._auto_last_untranslated_fix_signatures.add(fix_signature)
                self._auto_log(
                    f"Запускаю точечное исправление недоперевода для {len(fix_signature)} глав…",
                    force=True,
                    details_title="[AUTO] Точечный фикс недоперевода",
                    details_text=undertranslation_request_details or None,
                )
                auto_fix_result = dialog.run_auto_untranslated_fixer(
                    target_internal_paths=fix_signature,
                    provider_id=self.key_management_widget.get_selected_provider(),
                    active_keys=self.key_management_widget.get_active_keys(),
                    session_settings=self._get_effective_auto_model_settings(auto_settings),
                    batch_size=50,
                    save_immediately=True,
                )

        if dialog:
            dialog.deleteLater()
        self._auto_validator_dialog = None
        self._auto_followup_running = False

        if chapters_to_fix_untranslated:
            if auto_fix_result and auto_fix_result.get('success'):
                affected_paths = auto_fix_result.get('affected_internal_paths') or tuple(sorted(chapters_to_fix_untranslated))
                details_text = (
                    auto_fix_result.get('response_details_text')
                    or auto_fix_result.get('request_details_text')
                    or undertranslation_request_details
                    or None
                )
                self._auto_log(
                    "Точечный фикс недоперевода завершён: "
                    f"групп изменено {auto_fix_result.get('groups_changed', 0)}, "
                    f"замен {auto_fix_result.get('replacements', 0)}, "
                    f"сохранено файлов {auto_fix_result.get('saved_count', 0)}.",
                    force=True,
                    details_title="[AUTO] Точечно изменённые главы",
                    details_text=self._merge_auto_details(
                        details_text,
                        self._compose_auto_details([
                            ("Изменённые главы", list(affected_paths) if affected_paths else []),
                        ]),
                    ),
                )
            else:
                error_text = ""
                if auto_fix_result:
                    error_text = auto_fix_result.get('error', '')
                details_text = None
                if auto_fix_result:
                    details_text = (
                        auto_fix_result.get('response_details_text')
                        or auto_fix_result.get('request_details_text')
                    )
                if not details_text:
                    details_text = undertranslation_request_details or None
                self._auto_log(
                    "Точечный фикс недоперевода не выполнен."
                    + (f" Причина: {error_text}" if error_text else ""),
                    force=True,
                    details_title="[AUTO] Точечный фикс недоперевода",
                    details_text=details_text,
                )
                if not chapters_to_retry:
                    self._finish_auto_validator_followup(
                        auto_settings,
                        "Продолжаю автопайплайн без точечного фикса недоперевода.",
                    )
                    return

        if chapters_to_fix_untranslated and auto_fix_result and auto_fix_result.get('success') and not chapters_to_retry:
            self._auto_followup_running = True
            self.start_btn.setEnabled(False)
            self._auto_log("Перезапускаю автопроверку после точечного исправления недоперевода…", force=True)
            QtCore.QTimer.singleShot(250, lambda: self._run_auto_validator_followup(auto_settings))
            return

        if chapters_to_retry:
            signature = tuple(sorted(chapters_to_retry))
            if signature in self._auto_last_retry_signatures:
                self._auto_log(
                    "Получен тот же набор глав для повторного перевода. Автоцикл остановлен: "
                    f"{self._format_auto_chapter_list(signature, limit=10)}.",
                    force=True
                )
                self._reset_auto_workflow_state()
                self.check_ready()
                return

            self._auto_last_retry_signatures.add(signature)
            self.add_files_for_retry(self.selected_file, list(signature))
            cjk_retries = sum(1 for _, _, profile in ratio_profiles.values() if profile == "CJK")
            alpha_retries = max(0, len(signature) - cjk_retries)
            details_chunks = []
            if cjk_retries:
                details_chunks.append(f"CJK: {cjk_retries}")
            if alpha_retries:
                details_chunks.append(f"алфавитные: {alpha_retries}")
            ratio_details = []
            for path in signature:
                ratio_value, ratio_limit, profile = ratio_profiles.get(path, (None, None, None))
                if isinstance(ratio_value, (int, float)) and isinstance(ratio_limit, (int, float)):
                    ratio_details.append(
                        f"{self._short_auto_name(path)} ({ratio_value:.2f} < {ratio_limit:.2f}, {profile or 'общий'})"
                    )
                else:
                    ratio_details.append(self._short_auto_name(path))
            self._auto_log(
                f"Автовалидатор вернул на повтор {len(signature)} глав "
                f"(проверено: {total_scanned}, проблем: {suspicious_found})"
                + (f"; профили: {', '.join(details_chunks)}" if details_chunks else "")
                + ".",
                force=True,
                details_title="[AUTO] Повтор по ratio",
                details_text=self._compose_auto_details([
                    ("Профили", details_chunks),
                    ("Главы", ratio_details),
                ]),
            )
            if auto_settings.get('auto_restart_after_retry', True):
                self._auto_workflow_round += 1
                self._auto_followup_running = True
                self.start_btn.setEnabled(False)
                QtCore.QTimer.singleShot(250, lambda: self._start_translation(is_auto_restart=True))
            else:
                self._auto_log("Главы подготовлены к повтору, но автоперезапуск выключен.", force=True)
                self._reset_auto_workflow_state()
                self.check_ready()
            return

        self._auto_log("Автовалидатор не нашёл глав для повтора.", force=True)
        if auto_settings.get('ai_consistency_enabled'):
            self._run_auto_consistency_followup(auto_settings)
            return

        self._reset_auto_workflow_state()
        self.check_ready()

    def _run_auto_consistency_followup(self, auto_settings: dict):
        chapters_to_analyze = load_project_chapters_for_consistency(self.project_manager)
        active_keys = self.key_management_widget.get_active_keys()

        if not chapters_to_analyze:
            self._auto_log("AI-consistency пропущен: не найдено переведённых глав.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
            return

        if not active_keys:
            self._auto_log("AI-consistency пропущен: нет активных ключей для сессии.", force=True)
            self._reset_auto_workflow_state()
            self.check_ready()
            return

        config = self._get_effective_auto_model_settings(auto_settings)
        selected_confidences = auto_settings.get('ai_consistency_fix_confidences')
        if not isinstance(selected_confidences, (list, tuple, set)):
            selected_confidences = ['high', 'medium', 'low']
        selected_confidences = [
            str(level).strip().lower()
            for level in selected_confidences
            if str(level).strip().lower() in ('high', 'medium', 'low')
        ]
        config.update({
            'provider': self.key_management_widget.get_selected_provider(),
            'chunk_size': int(auto_settings.get('ai_consistency_chunk_size', 3)),
            'consistency_fix_confidences': list(selected_confidences),
        })

        self._auto_followup_running = True
        self.start_btn.setEnabled(False)
        self._auto_log("Запускаю AI-проверку согласованности…", force=True)
        self._auto_log(
            f"AI-consistency анализирует {len(chapters_to_analyze)} глав: "
            f"{self._format_auto_chapter_list([chapter.get('name') for chapter in chapters_to_analyze], limit=10, preserve_order=True)}",
        )
        if auto_settings.get('ai_consistency_auto_fix', True):
            fix_levels_text = ", ".join(selected_confidences) if selected_confidences else "ничего не исправлять"
            self._auto_log(f"AI-consistency автофикс по уровням уверенности: {fix_levels_text}.")

        worker = AutoConsistencyWorker(
            self.settings_manager,
            chapters_to_analyze,
            config,
            active_keys,
            auto_fix=bool(auto_settings.get('ai_consistency_auto_fix', True)),
            mode=auto_settings.get('ai_consistency_mode', 'standard'),
            parent=self,
        )
        worker.finished_with_result.connect(self._on_auto_consistency_finished)
        worker.failed.connect(self._on_auto_consistency_failed)
        worker.progress_message.connect(lambda message: self._auto_log(message))
        worker.finished.connect(lambda: setattr(self, '_auto_consistency_worker', None))
        self._auto_consistency_worker = worker
        worker.start()

    def _on_auto_consistency_finished(self, result: dict):
        self._auto_followup_running = False
        if self.project_manager:
            self.project_manager.reload_data_from_disk()

        problems_count = int(result.get('problems_count', 0))
        problems_by_confidence = result.get('problems_by_confidence') or {}
        fixed_count = int(result.get('fixed_count', 0))
        fixable_problems_count = int(result.get('fixable_problems_count', 0))
        auto_fix = bool(result.get('auto_fix', False))
        selected_confidences = result.get('selected_confidences') or []
        problem_chapters = result.get('problem_chapters') or []
        fixable_problem_chapters = result.get('fixable_problem_chapters') or []
        fixed_chapters = result.get('fixed_chapters') or []
        trace_details = self._compose_auto_trace_details(result.get('request_response_trace') or [])
        confidence_summary = []
        for level in ('high', 'medium', 'low'):
            count = int(problems_by_confidence.get(level, 0) or 0)
            if count:
                confidence_summary.append(f"{level}: {count}")
        confidence_suffix = f" ({', '.join(confidence_summary)})" if confidence_summary else ""

        if auto_fix and fixed_count:
            success_sections = []
            if selected_confidences:
                success_sections.append(("Исправляемые уровни", list(selected_confidences)))
            if fixed_chapters:
                success_sections.append(("Исправленные главы", list(fixed_chapters)))
            details_text = self._merge_auto_details(
                trace_details,
                self._compose_auto_details(success_sections),
            )
            self._auto_log(
                f"AI-consistency завершён: исправлено и сохранено {fixed_count} глав."
                f" Найдено проблем {problems_count}{confidence_suffix}.",
                force=True
                ,
                details_title="[AUTO] AI-consistency: результат",
                details_text=details_text or None,
            )
        else:
            result_sections = []
            if auto_fix:
                if selected_confidences:
                    result_sections.append(("Уровни автоисправления", list(selected_confidences)))
                    result_sections.append(("Кандидаты на автоисправление", [
                        f"Проблем: {fixable_problems_count}",
                    ]))
                else:
                    result_sections.append(("Автоисправление", [
                        "Не запускалось: не выбран ни один уровень уверенности.",
                    ]))
            if problem_chapters:
                result_sections.append(("Проблемные главы", list(problem_chapters)))
            if auto_fix and fixable_problem_chapters:
                result_sections.append(("Главы-кандидаты на автоисправление", list(fixable_problem_chapters)))
            details_text = self._merge_auto_details(
                trace_details,
                self._compose_auto_details(result_sections),
            )
            self._auto_log(
                f"AI-consistency завершён: найдено проблем {problems_count}{confidence_suffix}.",
                force=True,
                details_title="[AUTO] AI-consistency: результат",
                details_text=details_text or None,
            )

        self._reset_auto_workflow_state()
        self.check_ready()

    def _on_auto_consistency_failed(self, error_text: str):
        self._auto_followup_running = False
        self._auto_log(f"AI-consistency завершился ошибкой: {error_text}", force=True)
        self._reset_auto_workflow_state()
        self.check_ready()

    def get_settings(self):
        active_keys = self.key_management_widget.get_active_keys()
        provider_id = self.key_management_widget.get_selected_provider()

        glossary_list = self.glossary_widget.get_glossary()
        full_glossary_data = {}
        for entry in glossary_list:
            original = str(entry.get('original') or "").strip()
            if not original:
                continue
            full_glossary_data[original] = {
                'rus': str((entry.get('rus') or entry.get('translation')) or ""),
                'note': str(entry.get('note') or "")
            }

        model_settings = self.model_settings_widget.get_settings()
        translation_options = self.translation_options_widget.get_settings()

        model_name = model_settings.get('model')
        model_config = api_config.all_models().get(model_name)

        settings = {
            'provider': provider_id,
            'model_config': model_config,
            'file_path': self.selected_file,
            'output_folder': self.output_folder,
            'api_keys': active_keys,
            'full_glossary_data': full_glossary_data,
            'custom_prompt': self.preset_widget.get_prompt() or api_config.default_prompt(),
            'auto_translation': self.auto_translate_widget.get_settings(),
            'auto_start': True,
            'num_instances': self.instances_spin.value(),
        }

        if self.output_folder:
            project_manager = TranslationProjectManager(self.output_folder)
            settings['project_manager'] = project_manager

        settings.update(model_settings)
        settings.update(translation_options)

        if settings.get('sequential_translation'):
            try:
                chapter_order = get_epub_chapter_order(self.selected_file) if self.selected_file else []
            except Exception as exc:
                print(f"[SEQUENTIAL] Failed to read EPUB chapter order: {exc}")
                chapter_order = []

            if not chapter_order:
                chapter_order = self._unpack_tasks_to_chapters() or list(self.html_files or [])
            settings['sequential_chapter_order'] = chapter_order
            active_chapter_order = self._unpack_tasks_to_chapters() or list(self.html_files or [])
            chapter_chains = self._build_sequential_chapter_chains(
                active_chapter_order,
                settings.get('sequential_translation_splits', 1),
            )
            if chapter_chains:
                settings['sequential_translation_splits'] = len(chapter_chains)
            settings['sequential_chain_starts'] = [
                chain[0] for chain in chapter_chains if chain
            ]

        return settings


# gemini_translator\ui\dialogs\setup.py -> class InitialSetupDialog

    # --- ЗАМЕНИТЕ ЭТОТ МЕТОД ЦЕЛИКОМ НА ФИНАЛЬНУЮ ВЕРСИЮ ---
    def perform_dry_run(self):
        """
        Запускает пробный запуск, "замораживая" все задачи, кроме первой.
        """
        if not (self.engine and self.engine.task_manager and self.engine.task_manager.has_pending_tasks() ):
            QMessageBox.warning(self, "Ошибка", "Нет задач для пробного запуска.")
            return

        try:
            # 1. "Замораживаем" задачи
            self.engine.task_manager.hold_all_except_first()

            # 2. Получаем настройки и модифицируем их для dry_run
            settings = self.get_settings()
            dry_run_settings = settings.copy()
            dry_run_settings.update({
                'provider': 'dry_run', 'api_keys': ['dry_run_dummy_key'], 'num_instances': 1, 'rpm_limit': 1000
            })

            # 3. Запускаем сессию (остальное без изменений)
            self.dry_run_start_time = time.perf_counter()
            self._post_event(name='start_session_requested', data={'settings': dry_run_settings})

            self.dry_run_btn.setText("Обработка…")
            self.dry_run_btn.setEnabled(False)

        except Exception as e:
            # В случае ошибки, "размораживаем" задачи обратно
            if self.engine and self.engine.task_manager:
                self.engine.task_manager.release_held_tasks()

            QMessageBox.critical(self, "Ошибка запуска", f"Не удалось запустить пробный запуск:\n{e}")
            self.dry_run_btn.setText("Пробный запуск")
            self.dry_run_btn.setEnabled(True)


    def check_unvalidated_chapters(self):
        """Проверяет, какие главы уже переведены, и предлагает их исключить."""
        if not self.output_folder or not self.html_files: return

        # --- НАЧАЛО ИЗМЕНЕНИЯ: Используем новую, правильную логику ---
        from ...api import config as api_config

        validated_chapters, unvalidated_chapters, untranslated_chapters = set(), set(), []
        epub_base_name = os.path.splitext(os.path.basename(self.selected_file))[0]
        validated_folder = os.path.join(self.output_folder, "validated_ok")

        for html_file in self.html_files:
            safe_html_name = re.sub(r'[\\/*?:"<>|]', "_", os.path.splitext(os.path.basename(html_file))[0])
            base_filename = f"{epub_base_name}_{safe_html_name}"

            # 1. Приоритетная проверка: ищем готовую версию
            validated_filepath = os.path.join(validated_folder, f"{base_filename}_validated.html")
            if os.path.exists(validated_filepath):
                validated_chapters.add(html_file)
                continue

            # 2. Вторая проверка: ищем любую переведенную версию
            is_unvalidated = False
            for suffix in api_config.all_translated_suffixes():
                unvalidated_filepath = os.path.join(self.output_folder, f"{base_filename}{suffix}")
                if os.path.exists(unvalidated_filepath):
                    is_unvalidated = True
                    break

            if is_unvalidated:
                unvalidated_chapters.add(html_file)
            else:
                untranslated_chapters.append(html_file)


        if not validated_chapters and not unvalidated_chapters:
            return

        msg = QMessageBox()
        msg.setWindowTitle("Обнаружены переведенные главы")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Information)

        msg.setText(
            f"<b>Анализ выбранных глав ({len(self.html_files)}):</b>\n\n"
            f"✅ <font color='green'>Проверенные ('готовые'):</font> <b>{len(validated_chapters)}</b>\n"
            f"🔵 <font color='blue'>Непроверенные ('переведенные'):</font> <b>{len(unvalidated_chapters)}</b>\n"
            f"⚪ Непереведенные: <b>{len(untranslated_chapters)}</b>"
        )
        msg.setInformativeText("Выберите, какие главы вы хотите включить в текущую сессию перевода:")

        btn_skip_all = msg.addButton("Пропустить всё переведенное", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        btn_retranslate_unvalidated = msg.addButton("Перевести непроверенные", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        btn_retranslate_all = msg.addButton("Перевести всё заново", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)

        btn_skip_all.setToolTip("Будут переведены только непереведенные главы.")
        btn_retranslate_unvalidated.setToolTip("Перезапишет 'непроверенные', но сохранит 'готовые'.")
        btn_retranslate_all.setToolTip("Полностью перезапишет все существующие переводы.")

        msg.exec()

        clicked_button = msg.clickedButton()
        original_html_files = self.html_files.copy() # Сохраняем исходный выбор

        if clicked_button == btn_skip_all:
            self.html_files = untranslated_chapters
            info = f"Выбрано глав: {len(self.html_files)} (все переведенные пропущены)"
        elif clicked_button == btn_retranslate_unvalidated:
            self.html_files = untranslated_chapters + list(unvalidated_chapters)
            info = f"Выбрано глав: {len(self.html_files)} (пропущены только 'готовые')"
        elif clicked_button == btn_retranslate_all:
            self.html_files = original_html_files # Возвращаем исходный выбор
            info = f"Выбрано глав: {len(self.html_files)} (все главы будут переведены заново)"
        else:
            self.html_files, self.selected_file = [], None
            self.paths_widget.set_file_path(None)
            info = ""

        self._on_project_data_changed()


    def reject(self):
        """
        Перехватывает событие закрытия. Корректно проверяет наличие ИЗМЕНЕНИЙ
        и предлагает сохранить их только в этом случае.
        """
        if not self._prepare_for_close():
            return

        super().reject()


    # --------------------------------------------------------------------
    # ОСТАЛЬНЫЕ ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ (общие для обоих режимов)
    # --------------------------------------------------------------------

    def estimate_tokens(self):
        """Оценивает количество токенов для выбранных глав"""
        if not self.selected_file or not self.html_files:
            QMessageBox.warning(self, "Ошибка", "Сначала выберите файл и главы")
            return
        counter = TokenCounter()
        prompt_text = self.custom_prompt_edit.toPlainText() or " "

        # Собираем данные из новой таблицы глоссария в одну строку,
        # чтобы симулировать текстовое представление для подсчета токенов.
        glossary_lines = []
        for row in range(self.glossary_table.rowCount()):
            original_item = self.glossary_table.item(row, 0)
            translation_item = self.glossary_table.item(row, 1)

            original = original_item.text().strip() if original_item else ""
            rus = translation_item.text().strip() if translation_item else ""

            if original and rus:
                glossary_lines.append(f"{original} = {rus}")

        glossary_text = "\n".join(glossary_lines)

        try:
            with zipfile.ZipFile(open(self.selected_file, 'rb'), 'r') as epub_zip:
                for html_file in self.html_files[:10]:
                    try:
                        html_content = epub_zip.read(html_file).decode('utf-8', errors='ignore')
                        counter.add_chapter_stats(
                            chapter_name=os.path.basename(html_file),
                            html_size=len(html_content),
                            prompt_size=len(prompt_text),
                            glossary_size=len(glossary_text),
                            estimated_output=len(html_content)
                        )
                    except Exception as e:
                        print(f"Ошибка при оценке главы {html_file}: {e}")
            if counter.chapters_stats:
                report = counter.get_estimation_report(num_windows=len(self.api_keys))
                dialog = QDialog(self)
                dialog.setWindowTitle("Оценка токенов")
                dialog.setMinimumSize(600, 500)
                layout = QVBoxLayout(dialog)
                text_edit = QTextEdit()
                text_edit.setReadOnly(True)
                text_edit.setFont(QtGui.QFont("Consolas", 10))
                text_edit.setPlainText(report)
                close_btn = QPushButton("Закрыть")
                close_btn.clicked.connect(dialog.accept)
                layout.addWidget(text_edit)
                layout.addWidget(close_btn)
                dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось оценить токены: {e}")


    @QtCore.pyqtSlot()
    def _on_project_data_changed(self, offer_snapshot_restore=True, rebuild_tasks=True):
        """
        Единый метод-оркестратор. Вызывается при любом изменении
        основных данных проекта. Централизованно управляет загрузкой
        глоссария и обновлением всего UI.
        """
        print("[DEBUG] Сработал оркестратор _on_project_data_changed")
        if hasattr(self, 'glossary_widget'):
            self.glossary_widget.set_project_path(self.output_folder)

        # --- "УМНАЯ" ЗАГРУЗКА ГЛОССАРИЯ (ЦЕНТРАЛИЗОВАННАЯ) ---
        if self.output_folder and self.output_folder != self.current_project_folder_loaded:
            print(f"[INFO] Обнаружена смена проекта. Загрузка глоссария для: {os.path.basename(self.output_folder)}")
            self._load_project_glossary(self.output_folder)
            self.current_project_folder_loaded = self.output_folder
        # --------------------------------------------------------

        # 1. Обновляем данные о главах в виджете опций (это быстро и нужно для расчетов)
        self.translation_options_widget.update_chapter_data(self.html_files, self.selected_file, self.project_manager)

        # 2. Обновляем CJK-опции на основе новых данных о главах
        self._update_cjk_options_for_widgets()

        # 3. Пересобираем список задач
        if rebuild_tasks:
            self._prepare_and_display_tasks(clean_rebuild=True)
        self.paths_widget.update_chapters_info(len(self.html_files))
        # 4. Вызываем пересчет рекомендаций, так как данные о главах изменились
        self._update_recommendations()
        self._refresh_auto_translate_runtime_context()

        # 5. Обновляем все остальные зависимые UI элементы
        self.check_ready()
        self._update_distribution_info_from_widget()
        # Предложение восстановить снимок очереди уместно только при загрузке
        # проекта, а не при локальных операциях вроде фильтрации/пересборки списка.
        if offer_snapshot_restore:
            self._maybe_offer_snapshot_restore()

        if (hasattr(self, 'tabs_group') and
                self.tabs_group.currentIndex() == getattr(self, 'glossary_tab_index', -1)):
            QtCore.QTimer.singleShot(0, self._maybe_offer_base_glossaries_for_empty_project)

        # --- НОВАЯ ЛОГИКА ДЛЯ КНОПКИ-МЕТАМОРФА ---
        is_project_defined = bool(self.selected_file and self.output_folder)
        self.use_project_settings_btn.setVisible(is_project_defined)

        if not is_project_defined and self.use_project_settings_btn.isChecked():
            self.use_project_settings_btn.setChecked(False)

        self._update_context_button_style(self.use_project_settings_btn.isChecked())

    def _toggle_project_settings_mode(self, use_local):
        """
        Переключает UI между глобальными настройками и настройками проекта,
        НЕ затрагивая глоссарий. Использует self.settings_manager для глобальных операций.
        """
        is_currently_local = not use_local

        if self.is_settings_dirty:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Question)
            msg_box.setWindowTitle("Несохраненные изменения")

            if is_currently_local:
                msg_box.setText("Вы изменили настройки текущего проекта.")
                msg_box.setInformativeText("Сохранить изменения в файл 'project_settings.json' перед переключением на глобальные?")
                save_btn_text = "Сохранить в Проект"
            else:
                msg_box.setText("Вы изменили глобальные настройки.")
                msg_box.setInformativeText("Перезаписать глобальные настройки перед переключением на проект?")
                save_btn_text = "Перезаписать Глобальные"

            save_btn = msg_box.addButton(save_btn_text, QMessageBox.ButtonRole.AcceptRole)
            discard_btn = msg_box.addButton("Не сохранять", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
            msg_box.exec()
            clicked = msg_box.clickedButton()

            if clicked == save_btn:
                if is_currently_local:
                    self._save_project_settings_only()
                else:
                    self._save_global_ui_settings()
            elif clicked == cancel_btn:
                self.use_project_settings_btn.blockSignals(True)
                self.use_project_settings_btn.setChecked(is_currently_local)
                self.use_project_settings_btn.blockSignals(False)
                return

        # --- Основная логика ЗАГРУЗКИ (БЕЗ глоссария) ---
        if use_local:
            print("[SETTINGS] Переключение на настройки проекта…")
            project_settings_path = os.path.join(self.output_folder, "project_settings.json")
            if os.path.exists(project_settings_path):
                local_manager = SettingsManager(config_file=project_settings_path)
                local_settings = local_manager.load_full_session_settings()
                self.global_settings = self._get_full_ui_settings()
                self._apply_full_ui_settings(local_settings)
                self.local_set = True
            else:
                print("[INFO] Файл настроек проекта не найден. Используются текущие настройки UI.")
        else:
            print("[SETTINGS] Переключение на глобальные настройки…")
            if self.global_settings:
                self._apply_full_ui_settings(self.global_settings)
            self.local_set = False
        # Сбрасываем флаг "грязных" настроек ПОСЛЕ любого переключения.
        # Теперь это работает корректно, т.к. _apply_full_ui_settings не генерирует сигналы.
        self.is_settings_dirty = False
        self.setWindowTitle(self.windowTitle().replace("*", ""))

        self._update_context_button_style(use_local)

    def _handle_task_reanimation(self, task_ids: list):
        if self.engine and self.engine.task_manager:
            # --- ПЕРЕНОСИМ В ФОНОВЫЙ ПОТОК ---
            self.task_management_widget.setEnabled(False)
            self.status_bar.set_permanent_message("Обновление статусов...")

            self.db_worker = TaskDBWorker(self.engine.task_manager.reanimate_tasks, task_ids)
            self.db_worker.finished.connect(self._on_db_worker_finished)
            self.db_worker.start()

    def _unpack_tasks_to_chapters(self):
        """
        Извлекает все главы из АКТУАЛЬНОГО списка задач в TaskManager,
        СОХРАНЯЯ ИХ ТОЧНЫЙ ПОРЯДОК, корректно "схлопывая" чанки
        и СОХРАНЯЯ намеренные дубликаты глав.
        """
        if not (self.engine and self.engine.task_manager):
            return []

        tasks_with_uuid = self.engine.task_manager.get_all_pending_tasks()

        unpacked_chapters_in_order = []
        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Отслеживаем ПОСЛЕДНЮЮ добавленную главу ---
        last_added_chapter_from_chunk = None

        for task_id, task_payload in tasks_with_uuid:
            task_type = task_payload[0]

            if task_type == 'epub_chunk':
                chapter_path = task_payload[2]
                # Если текущий чанк относится к той же главе, что и предыдущий,
                # мы его просто ИГНОРИРУЕМ.
                if chapter_path == last_added_chapter_from_chunk:
                    continue
                else:
                    # Если это чанк от НОВОЙ главы, добавляем его и запоминаем.
                    unpacked_chapters_in_order.append(chapter_path)
                    last_added_chapter_from_chunk = chapter_path

            elif task_type == 'epub':
                chapter_path = task_payload[2]
                unpacked_chapters_in_order.append(chapter_path)
                # Сбрасываем "память о чанках", так как следующая задача может быть чанком
                last_added_chapter_from_chunk = None

            elif task_type == 'epub_batch':
                # Для пакетов просто добавляем все главы как есть, включая дубликаты
                unpacked_chapters_in_order.extend(task_payload[2])
                # Сбрасываем "память о чанках"
                last_added_chapter_from_chunk = None

        return unpacked_chapters_in_order

    def _update_context_button_style(self, is_local_mode):
        """Обновляет текст, подсказку и стиль кнопки контекста."""
        if is_local_mode:
            self.use_project_settings_btn.setText("Настройки проекта")
            self.use_project_settings_btn.setToolTip("Используются локальные настройки из файла project_settings.json\nНажмите, чтобы вернуться к глобальным.")

        else:
            self.use_project_settings_btn.setText("Глобальные настройки")
            self.use_project_settings_btn.setToolTip("Используются глобальные настройки из домашней директории.\nНажмите, чтобы переключиться на настройки проекта (будет создан файл, если его нет).")

    def update_keys_count(self):
        """Обновляет счетчик API ключей"""
        keys = [k.strip() for k in self.keys_edit.toPlainText().splitlines() if k.strip()]
        unique_keys = list(set(keys))


        num_keys = len(unique_keys)
        self.instances_spin.setMaximum(num_keys if num_keys > 0 else 1)


        if len(keys) != len(unique_keys):
            self.keys_count_label.setText(f"Ключей: {len(unique_keys)} (уникальных из {len(keys)})")
            self.keys_count_label.setStyleSheet("color: orange; font-size: 10px;")
        else:
            self.keys_count_label.setText(f"Ключей: {len(keys)}")
            self.keys_count_label.setStyleSheet("color: blue; font-size: 10px;")
        self._update_distribution_info() # <--- ДОБАВЬ ЭТУ СТРОКУ


    def update_glossary_count(self):
        """Обновляет счетчик терминов в глоссарии"""
        self.glossary_count_label.setText(f"Терминов: {self.glossary_table.rowCount()}")


    def _init_lazy_ui_skeleton(self):
        """Создает минимальный 'скелет' UI для мгновенного отображения."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.loading_label = QLabel("<h2>Загрузка интерфейса…</h2>")
        self.loading_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.loading_label)

        # Основной контейнер, который будет заполнен позже
        self.main_content_widget = QWidget()
        self.main_content_widget.setVisible(False)
        main_layout.addWidget(self.main_content_widget, 1)

    def showEvent(self, event):
        """
        Перехватывает событие первого показа окна и запускает отложенную
        загрузку тяжелых компонентов UI.
        """
        super().showEvent(event)

        # Если пока мы были скрыты/свернуты, сессия началась или закончилась — синхронизируемся.


        if not self._initial_show_done:
            self._initial_show_done = True

            # --- Отложенный запуск ---
            # QTimer.singleShot(0, …) выполнит функцию в следующем цикле событий,
            # дав Qt время полностью отрисовать текущее окно.
            QtCore.QTimer.singleShot(50, self._async_populate_and_load)
        else:
            self._check_and_sync_active_session()

    def _check_and_sync_active_session(self):
        """
        Принудительно проверяет наличие активной сессии в глобальном состоянии (EventBus/Engine).
        Используется для восстановления UI, если событие 'session_started' было пропущено.
        Возвращает True, если сессия активна (и UI был синхронизирован), иначе False.
        """
        # 1. Спрашиваем у Шины (Главный источник правды)
        active_session_id = None
        if self.bus and hasattr(self.bus, 'get_data'):
            active_session_id = self.bus.get_data("current_active_session")

        # 2. Если Шина молчит, спрашиваем у Движка напрямую (Резерв)
        if not active_session_id and self.engine and self.engine.session_id:
             active_session_id = self.engine.session_id

        # 3. АНАЛИЗ: Если сессия ЕСТЬ, но мы думаем, что СПИМ (is_session_active=False)
        if active_session_id and not self.is_session_active:
            print(f"[UI RECOVERY] ⚠️ Обнаружена рассинхронизация! Сессия {active_session_id} работает, а диалог спит. Блокирую интерфейс.")
            self.is_session_active = True

            # Принудительно переводим UI в режим "Сессия идет" (блокируем инпуты, включаем Стоп)
            self._set_controls_enabled(False)

            # Если это первая синхронизация, обновляем статус бар с актуальным количеством задач
            if self.status_bar:
                current_total = 0
                if self.task_manager:
                    # Получаем актуальное количество задач из менеджера (восстанавливаем контекст)
                    try:
                        current_total = len(self.task_manager.get_ui_state_list())
                    except Exception:
                        current_total = 0
                self.status_bar.start_session(current_total)

            return True

        # 4. Если сессия ЕСТЬ и мы ЗНАЕМ об этом — просто подтверждаем статус
        if active_session_id and self.is_session_active:
            self._set_controls_enabled(False)
            return True

        # Сессии нет
        self._set_controls_enabled(True)
        return False

    def _async_populate_and_load(self):
        """Асинхронный orchestrator: сначала строит UI, потом загружает данные."""
        # 1. Создаем все тяжелые виджеты
        self._populate_full_ui()

        # 2. Загружаем данные в уже созданные виджеты
        self._load_initial_data()

        # 3. "Подменяем" заглушку на готовый интерфейс
        self.loading_label.setVisible(False)
        self.main_content_widget.setVisible(True)

    def _show_custom_message(self, title, text, icon=QMessageBox.Icon.Information, informative_text="", button_text="ОК"):
        """Показывает QMessageBox с кастомной кнопкой 'ОК'."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setIcon(icon)
        msg_box.setText(text)
        if informative_text:
            msg_box.setInformativeText(informative_text)
        # Добавляем свою кнопку с нужным текстом
        ok_button = msg_box.addButton(button_text, QMessageBox.ButtonRole.AcceptRole)
        msg_box.exec()

    def closeEvent(self, event):
        """Отписываемся от шины событий перед уничтожением окна."""
        if self._returning_to_main_menu:
            if self.bus:
                try:
                    self.bus.event_posted.disconnect(self.on_event)
                except (TypeError, RuntimeError):
                    pass
            return_to_main_menu()
            event.accept()
            return

        action = prompt_return_to_menu(self)
        if action == "cancel":
            event.ignore()
            return

        if not self._prepare_for_close():
            event.ignore()
            return

        if self.bus:
            try:
                self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError):
                pass # Соединение уже могло быть разорвано

        if action == "menu":
            return_to_main_menu()
        event.accept()
