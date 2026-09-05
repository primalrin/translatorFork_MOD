# -*- coding: utf-8 -*-

import os
import json
from PyQt6 import QtWidgets, QtCore
from PyQt6.QtWidgets import (
    QListWidget, QFileDialog, QDialog, QVBoxLayout, QPushButton, QLabel, 
    QGroupBox, QDialogButtonBox, QCheckBox, QLineEdit, QHBoxLayout, 
    QComboBox, QTextEdit, QSpinBox, QGridLayout, QMessageBox, QStyle, 
    QListWidgetItem
)
from PyQt6.QtCore import pyqtSignal
from ...utils import markdown_viewer
from gemini_translator.ui import theme_manager

# --- НОВЫЙ ВСПОМОГАТЕЛЬНЫЙ КЛАСС ДЛЯ СПИСКОВ ---
class CustomListWidget(QListWidget):
    """Кастомный QListWidget, который реагирует на нажатие клавиши Delete."""
    delete_pressed = pyqtSignal()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.delete_pressed.emit()

# --- НОВЫЙ ДИАЛОГ ДЛЯ ВВОДА КЛЮЧЕЙ ---
class KeyInputDialog(QDialog):
    """Кастомный диалог для ввода ключей с кнопкой загрузки из файла."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить/обновить ключи")
        layout = QVBoxLayout(self)
        
        info_label = QLabel("Вставьте ключи (по одному на строку) или загрузите из файла.")
        layout.addWidget(info_label)

        self.keys_edit = QTextEdit()
        self.keys_edit.setProperty("selectionTranslationDisabled", True)
        layout.addWidget(self.keys_edit)

        button_layout = QHBoxLayout()
        self.load_from_file_btn = QPushButton("📁 Загрузить из файла…")
        self.load_from_file_btn.clicked.connect(self.load_from_file)
        
        button_layout.addWidget(self.load_from_file_btn)
        button_layout.addStretch()

        self.dialog_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_button = self.dialog_buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Принять")
        cancel_button = self.dialog_buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("Отмена")
        self.dialog_buttons.accepted.connect(self.accept)
        self.dialog_buttons.rejected.connect(self.reject)
        
        button_layout.addWidget(self.dialog_buttons)
        layout.addLayout(button_layout)
        
    def get_text(self):
        return self.keys_edit.toPlainText()

    def load_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл с ключами (.txt)", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.keys_edit.setPlainText(content)
                QMessageBox.information(self, "Успех", f"Ключи из файла '{os.path.basename(file_path)}' успешно загружены.")
            except Exception as e:
                QMessageBox.warning(self, "Ошибка чтения", f"Не удалось прочитать файл:\n{e}")

# --- НОВЫЙ ДИАЛОГ ДЛЯ УДАЛЕНИЯ КЛЮЧЕЙ ---
class DeleteKeysDialog(QDialog):
    """Кастомный диалог для выбора режима удаления ключей."""
    def __init__(self, parent=None, provider_name="не указан", num_selected=0, num_provider=0, num_total=0):
        super().__init__(parent)
        self.setWindowTitle("Подтверждение удаления ключей")
        self.choice = None
        layout = QVBoxLayout(self)
        
        main_label = QLabel("Выберите, какие ключи вы хотите навсегда удалить из пула:")
        layout.addWidget(main_label)

        self.delete_selected_btn = QPushButton(f"Удалить только выделенные ({num_selected})")
        self.delete_selected_btn.clicked.connect(lambda: self._set_choice_and_accept('selected'))
        self.delete_selected_btn.setEnabled(num_selected > 0)
        layout.addWidget(self.delete_selected_btn)
        
        self.delete_provider_btn = QPushButton(f"Удалить ВСЕ для '{provider_name}' ({num_provider})")
        self.delete_provider_btn.clicked.connect(lambda: self._set_choice_and_accept('provider'))
        self.delete_provider_btn.setEnabled(num_provider > 0)
        layout.addWidget(self.delete_provider_btn)
        
        self.delete_all_btn = QPushButton(f"Удалить ВСЕ ключи ({num_total})")
        self.delete_all_btn.setStyleSheet(f"background-color: {theme_manager.color('danger')}; color: {theme_manager.color('accent_text')};")
        self.delete_all_btn.clicked.connect(lambda: self._set_choice_and_accept('all'))
        self.delete_all_btn.setEnabled(num_total > 0)
        layout.addWidget(self.delete_all_btn)
        
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(cancel_button, 0, QtCore.Qt.AlignmentFlag.AlignRight)

    def _set_choice_and_accept(self, choice):
        self.choice = choice
        self.accept()

class StartupToolDialog(QDialog):
    """
    Новый стартовый диалог для выбора основного инструмента: Переводчик,
    Валидатор или Менеджер глоссариев.
    """
    def __init__(self, parent=None, app_version=None):
        super().__init__(parent)
        
        # Устанавливаем заголовок с версией
        title = "Gemini EPUB Translator"
        if app_version:
            title += f" {app_version}"
        self.setWindowTitle(title)
        
        self.selected_tool = None
        self.setMinimumWidth(600)
        self.help_window = None
        app = QtWidgets.QApplication.instance()
        self.settings_manager = app.get_settings_manager()
        self.bus = app.event_bus  # Get the bus
        self.proxy_label = QLabel("")  # Инициализируем proxy_label как пустой
        self.init_ui()
        self.bus.event_posted.connect(self.on_event)
        self.activate_proxy_from_settings()  #  Запускаем применение настроек
    
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        
        # --- Верхняя панель с заголовком и кнопкой помощи ---
        top_panel_layout = QHBoxLayout()
        title = QLabel("<h2>Выберите основной инструмент для запуска:</h2>")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        
        self.help_button = QPushButton("[?]")
        self.help_button.setFixedSize(28, 28)
        self.help_button.setToolTip("Открыть справку")
        self.help_button.setStyleSheet("font-size: 14pt; border-radius: 14px;")
        self.help_button.clicked.connect(self.show_help_dialog)
        
        top_panel_layout.addWidget(title, 1) # Растягиваем заголовок
        top_panel_layout.addWidget(self.help_button, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        
        main_layout.addLayout(top_panel_layout)
        # --- Конец верхней панели ---

        translator_widget = self._create_tool_widget(
            "🚀 Переводчик EPUB",
            "Основной модуль для перевода книг. Запускайте многопоточную обработку глав, используя API Gemini, OpenRouter и GLM, с полным контролем над промптом, глоссарием и пакетными задачами.",
            "translator",
            is_large=True
        )
        main_layout.addWidget(translator_widget)

        bottom_layout = QHBoxLayout()

        validator_widget = self._create_tool_widget(
            "✅ Валидатор переводов",
            "Интерактивная вычитка и доработка перевода. Сравнивайте текст и HTML-код бок о бок, исправляйте ошибки, найденные автоматическими проверками, и помечайте главы как готовые к сборке.",
            "validator"
        )
        glossary_widget = self._create_tool_widget(
            "📚 Менеджер глоссариев",
            "Мощный редактор для профессиональной работы с терминами. Поручите AI найти и устранить все конфликты в глоссарии или используйте ручной пошаговый режим для детальной доработки.",
            "glossary"
        )
        rulate_export_widget = self._create_tool_widget(
            "📝 EPUB -> Rulate MD",
            "Конвертер EPUB в markdown-формат для Rulate: выбор глав, переименование, тома, платность и экспорт в один или несколько .md файлов.",
            "rulate_export"
        )
        chapter_splitter_widget = self._create_tool_widget(
            "✂️ Сплиттер глав",
            "Разбивает большие главы на части для EPUB и Rulate Markdown, автоматически дописывая '(Часть N)' и сохраняя структуру результата.",
            "chapter_splitter"
        )
        bottom_layout.addWidget(validator_widget)
        bottom_layout.addWidget(glossary_widget)
        bottom_layout.addWidget(rulate_export_widget)
        bottom_layout.addWidget(chapter_splitter_widget)
        main_layout.addLayout(bottom_layout)
        extra_tools_layout = QHBoxLayout()
        gemini_reader_widget = self._create_tool_widget(
            "🎧 Gemini Reader",
            "Озвучивание EPUB через Gemini Live с просмотром глав, очередью воркеров и экспортом MP3 в общем runtime приложения.",
            "gemini_reader"
        )
        extra_tools_layout.addWidget(gemini_reader_widget)
        ranobelib_widget = self._create_tool_widget(
            "RanobeLib Uploader",
            "Launches the external uploader for RanobeLib chapter uploads, login, and Rulate workflows.",
            "ranobelib_uploader"
        )
        extra_tools_layout.addWidget(ranobelib_widget)
        qidian_rulate_widget = self._create_tool_widget(
            "Qidian/Fanqie/Ciweimao/Qimao -> Rulate",
            "Создание черновика книги на Rulate: данные с Qidian, Fanqie, Ciweimao или Qimao, AI-перевод названия и описания, жанры, теги и автозаполнение формы.",
            "qidian_rulate_creator"
        )
        extra_tools_layout.addWidget(qidian_rulate_widget)
        benchmark_widget = self._create_tool_widget(
            "Бенчмарк промптов",
            "Сравнение промптов и моделей на фиксированных фрагментах: сборка prompt-only, live-запуск через API и отчёты JSON/CSV/Markdown.",
            "prompt_benchmark"
        )
        extra_tools_layout.addWidget(benchmark_widget)
        extra_tools_layout.addStretch()
        main_layout.addLayout(extra_tools_layout)

        # --- Блок информации о прокси и кнопка (внизу) ---
        proxy_layout = QHBoxLayout()
        self.proxy_label.setToolTip("")
        proxy_layout.addWidget(self.proxy_label, 1)

        self.proxy_button = QPushButton("Прокси")
        self.proxy_button.clicked.connect(self.open_proxy_settings)
        proxy_layout.addWidget(self.proxy_button, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        main_layout.addLayout(proxy_layout)

    def _create_tool_widget(self, title, description, tool_id, is_large=False):
        widget = QtWidgets.QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        button = QPushButton(title)
        font = button.font()

        if is_large:
            button.setMinimumHeight(60)
            font.setPointSize(14)
            font.setBold(True)
        else:
            button.setMinimumHeight(45)
            font.setPointSize(11)

        button.setFont(font)
        button.clicked.connect(lambda: self._select_tool(tool_id))

        label = QLabel(description)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme_manager.color('text_secondary')};")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop)

        layout.addWidget(button)
        layout.addWidget(label)

        widget.setLayout(layout)
        return widget

    def _select_tool(self, tool):
        self.selected_tool = tool
        self.accept()

    def open_proxy_settings(self):

        from gemini_translator.ui.dialogs.proxy import ProxySettingsDialog
        from gemini_translator.ui.overlay_host import present_dialog

        dialog = ProxySettingsDialog(self, self.settings_manager)
        present_dialog(self, dialog)


    def _on_show(self, event):
        super().showEvent(event)
    
    def show_help_dialog(self):
        """
        Запускает окно справки в немодальном режиме, явно указывая,
        какой файл нужно открыть.
        """
        # Если окно уже открыто, просто активируем его
        if self.help_window and self.help_window.isVisible():
            self.help_window.activateWindow()
            return
        
        # Вызываем нашу новую функцию, передавая ей путь к файлу по умолчанию,
        # который мы импортировали из самого модуля markdown_viewer.
        self.help_window = markdown_viewer.show_markdown_viewer(
            parent_window=self,
            modal=False,
            file_path=markdown_viewer.HELP_FILE_PATH  # <-- Явно указываем файл!
        )
        
    @QtCore.pyqtSlot(dict)
    def on_event(self, event_data: dict):
        """Обрабатывает события, отправленные через EventBus."""
        event_name = event_data.get('event')
        if event_name == 'current_proxy_status':  # Изменено
            self.update_proxy_display(event_data.get('data', {}))  # Передаем данные из события

    def update_proxy_display(self, settings): #  Теперь принимает settings, полученные от контроллера
        """Обновляет отображение информации о прокси."""
        enabled = settings.get('enabled', False)
        proxy_type = settings.get('type', 'SOCKS5')
        host = settings.get('host', 'не настроен')
        port = settings.get('port', '')
        user = settings.get('user', '')
        password = settings.get('pass', '')
        if enabled:
            proxy_text = f"{proxy_type}://{host}:{port}"
            self.proxy_label.setText(proxy_text)
            self.proxy_label.setToolTip(f"Тип: {proxy_type}\nПользователь: {user}\nПароль: {password}")
            self.proxy_label.setStyleSheet(f"color: {theme_manager.color('success')};")
        else:
            self.proxy_label.setText("")
            self.proxy_label.setToolTip("")
            self.proxy_label.setStyleSheet("")
    
    def activate_proxy_from_settings(self): #  <-- Новый метод
        """Загружает настройки прокси из SettingsManager и применяет их."""
        settings = self.settings_manager.load_proxy_settings()
        #  Отправляем событие с настройками (как будто они были изменены)
        self.bus.event_posted.emit({
            'event': 'proxy_started',
            'source': 'StartupToolDialog',
            'data': settings
        })
    
    def show_pysocks_missing_message(self):
        """Отображает сообщение об отсутствии PySocks."""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Ошибка")
        msg_box.setText(
            "Для использования прокси необходимо установить библиотеку PySocks.\n"
            "Установите ее, выполнив в терминале:\n\n"
            "`pip install PySocks`"
        )
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok) # Кнопка Ok
        msg_box.exec()
        
    def closeEvent(self, event):
        """Отключаем обработчик событий перед закрытием."""
        if self.bus:  # Проверяем, что bus существует
            try:
                self.bus.event_posted.disconnect(self.on_event)
            except (TypeError, RuntimeError):  # Обрабатываем ошибки, если соединение уже разорвано
                pass
        super().closeEvent(event)

class EnhancedProjectHistoryDialog(QDialog):
    """Расширенный диалог выбора проектов: недавние и найденные в выбранной папке."""
    PROJECT_SCAN_MAX_DIRS = 3000
    PROJECT_SCAN_MAX_DEPTH = 3
    PROJECT_SCAN_SKIP_DIRS = {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        "__pycache__",
        ".venv",
        ".venv-linux",
        "venv",
        "env",
        "node_modules",
        "build",
        "dist",
    }

    def __init__(self, history, settings_manager=None, parent=None):
        super().__init__(parent)
        self.history = list(history or [])
        self.hidden_removed_folder_keys = set()
        self.selected_project = None
        self.projects_root_folder = ""
        self.all_projects = []
        self._scan_cache_root = None
        self._scan_cache_project_folders = []
        self._scan_status_message = ""
        self.setWindowTitle("История проектов")
        self.setMinimumSize(760, 500)

        if settings_manager is None:
            app = QtWidgets.QApplication.instance()
            if not hasattr(app, 'settings_manager'):
                raise RuntimeError("SettingsManager не найден.")
            self.settings_manager = app.get_settings_manager()
        else:
            self.settings_manager = settings_manager

        saved_root_folder = self.settings_manager.get_last_projects_root_folder()
        if saved_root_folder:
            self.projects_root_folder = os.path.normpath(saved_root_folder)

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Выберите проект для загрузки:"))

        root_layout = QHBoxLayout()
        root_layout.addWidget(QLabel("Папка со всеми проектами:"))
        self.root_folder_edit = QLineEdit()
        self.root_folder_edit.setReadOnly(True)
        if self.projects_root_folder:
            self.root_folder_edit.setText(self.projects_root_folder)
        self.root_folder_edit.setPlaceholderText("Не выбрана")
        root_layout.addWidget(self.root_folder_edit, 1)

        self.choose_root_button = QPushButton("Выбрать папку...")
        self.choose_root_button.clicked.connect(self.choose_projects_root_folder)
        root_layout.addWidget(self.choose_root_button)

        self.clear_root_button = QPushButton("Очистить")
        self.clear_root_button.clicked.connect(self.clear_projects_root_folder)
        root_layout.addWidget(self.clear_root_button)
        layout.addLayout(root_layout)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по имени папки проекта...")
        self.search_edit.textChanged.connect(self._refresh_project_list)
        layout.addWidget(self.search_edit)

        self.status_label = QLabel("Показаны недавние проекты.")
        self.status_label.setStyleSheet(f"color: {theme_manager.color('text_muted')};")
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemSelectionChanged.connect(self._update_delete_button_state)
        self.list_widget.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.list_widget)

        button_layout = QHBoxLayout()
        self.delete_button = QPushButton("Удалить из истории")
        self.delete_button.clicked.connect(self.delete_selected)
        self.delete_button.setEnabled(False)
        button_layout.addWidget(self.delete_button)
        button_layout.addStretch()

        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        load_button = QPushButton("Загрузить")
        load_button.clicked.connect(self.accept)
        button_layout.addWidget(load_button)
        layout.addLayout(button_layout)

    def _normalize_output_folder(self, path):
        return os.path.normcase(os.path.normpath(path or ""))

    def _find_epub_in_project_folder(self, folder_path):
        try:
            epub_files = sorted(
                os.path.join(folder_path, name)
                for name in os.listdir(folder_path)
                if name.lower().endswith(".epub")
            )
        except OSError:
            return None
        return epub_files[0] if epub_files else None

    def _build_history_lookup(self):
        lookup = {}
        for project in self.history:
            output_folder = project.get("output_folder")
            if output_folder:
                lookup[self._normalize_output_folder(output_folder)] = project
        return lookup

    def _build_display_name(self, project):
        folder_name = project.get("folder_name") or os.path.basename(
            self._normalize_output_folder(project.get("output_folder"))
        )
        if project.get("_from_history") and project.get("_from_scan"):
            return f"{folder_name} | recent | folder"
        if project.get("_from_history"):
            return f"{folder_name} | recent"
        if project.get("_from_scan"):
            return f"{folder_name} | folder"
        return folder_name or project.get("name", "Без имени")

    def _scan_root_is_too_broad(self, folder_path):
        try:
            normalized = os.path.abspath(os.path.normpath(folder_path))
        except (TypeError, ValueError, OSError):
            return True

        drive, _ = os.path.splitdrive(normalized)
        drive_root = os.path.abspath((drive + os.sep) if drive else os.sep)
        if os.path.normcase(normalized) == os.path.normcase(drive_root):
            return True

        try:
            home_dir = os.path.abspath(os.path.expanduser("~"))
        except (TypeError, ValueError, OSError):
            home_dir = ""
        if home_dir and os.path.normcase(normalized) == os.path.normcase(home_dir):
            return True

        return False

    def _scan_projects_root_folder(self):
        root_folder = os.path.normpath(self.projects_root_folder or "")
        if not root_folder or not os.path.isdir(root_folder):
            return [], ""

        if self._scan_root_is_too_broad(root_folder):
            return [], (
                "Папка для поиска проектов слишком общая. "
                "Выберите папку, где лежат только проекты."
            )

        root_abs = os.path.abspath(root_folder)
        project_folders = []
        visited_dirs = 0
        stopped_by_limit = False

        try:
            for current_root, dirs, files in os.walk(root_abs):
                visited_dirs += 1
                if visited_dirs > self.PROJECT_SCAN_MAX_DIRS:
                    stopped_by_limit = True
                    dirs[:] = []
                    break

                dirs[:] = [
                    dirname for dirname in dirs
                    if dirname not in self.PROJECT_SCAN_SKIP_DIRS
                ]

                relative_root = os.path.relpath(current_root, root_abs)
                depth = 0 if relative_root == "." else len(relative_root.split(os.sep))
                if depth >= self.PROJECT_SCAN_MAX_DEPTH:
                    dirs[:] = []

                # Проект — это и папка с одним лишь глоссарием: перевод мог
                # ещё не начинаться, но проект уже существует.
                if (
                    "translation_map.json" not in files
                    and "project_glossary.json" not in files
                ):
                    continue

                project_folders.append(os.path.normpath(current_root))
                dirs[:] = []
        except OSError as exc:
            return [], f"Не удалось просканировать папку проектов: {exc}"

        if stopped_by_limit:
            return project_folders, (
                f"Поиск остановлен после {self.PROJECT_SCAN_MAX_DIRS} папок. "
                "Выберите более узкую папку с проектами."
            )

        return project_folders, ""

    def _get_scanned_project_folders(self, force_scan=False):
        root_folder = os.path.normpath(self.projects_root_folder or "")
        if not force_scan and root_folder == self._scan_cache_root:
            return list(self._scan_cache_project_folders)

        project_folders, status_message = self._scan_projects_root_folder()
        self._scan_cache_root = root_folder
        self._scan_cache_project_folders = list(project_folders)
        self._scan_status_message = status_message
        return list(project_folders)

    def _clear_scan_cache(self):
        self._scan_cache_root = None
        self._scan_cache_project_folders = []
        self._scan_status_message = ""

    def _rebuild_projects(self, force_scan=False):
        history_lookup = self._build_history_lookup()
        projects_by_folder = {}

        for project in self.history:
            output_folder = project.get("output_folder")
            if not output_folder:
                continue

            normalized_output = self._normalize_output_folder(output_folder)
            if normalized_output in self.hidden_removed_folder_keys:
                continue

            project_copy = dict(project)
            project_copy["folder_name"] = os.path.basename(os.path.normpath(output_folder))
            project_copy["_from_history"] = True
            project_copy["_from_scan"] = False
            project_copy["_history_entry"] = project
            project_copy["_display_name"] = self._build_display_name(project_copy)
            projects_by_folder[normalized_output] = project_copy

        if self.projects_root_folder and os.path.isdir(self.projects_root_folder):
            for project_folder in self._get_scanned_project_folders(force_scan=force_scan):
                normalized_folder = self._normalize_output_folder(project_folder)
                if normalized_folder in self.hidden_removed_folder_keys:
                    continue
                history_project = history_lookup.get(normalized_folder)
                folder_name = os.path.basename(project_folder)

                if normalized_folder in projects_by_folder:
                    project_copy = projects_by_folder[normalized_folder]
                    project_copy["_from_scan"] = True
                    project_copy["folder_name"] = folder_name
                    project_copy["_display_name"] = self._build_display_name(project_copy)
                    continue

                epub_path = history_project.get("epub_path") if history_project else None
                if not epub_path or not os.path.exists(epub_path):
                    epub_path = self._find_epub_in_project_folder(project_folder)

                project_copy = {
                    "name": folder_name,
                    "folder_name": folder_name,
                    "epub_path": epub_path,
                    "output_folder": project_folder,
                    "_from_history": bool(history_project),
                    "_from_scan": True,
                    "_history_entry": history_project,
                }
                project_copy["_display_name"] = self._build_display_name(project_copy)
                projects_by_folder[normalized_folder] = project_copy

        self.all_projects = sorted(
            projects_by_folder.values(),
            key=lambda project: (
                0 if project.get("_from_history") else 1,
                (project.get("folder_name") or "").lower(),
                (project.get("output_folder") or "").lower(),
            )
        )

    def _refresh_project_list(self, *args, force_scan=False):
        self._rebuild_projects(force_scan=force_scan)
        search_text = self.search_edit.text().strip().lower()

        self.list_widget.clear()
        visible_projects = []
        for project in self.all_projects:
            folder_name = (project.get("folder_name") or "").lower()
            if search_text and search_text not in folder_name:
                continue

            item = QListWidgetItem(project.get("_display_name", project.get("name", "Без имени")))
            item.setToolTip(project.get("output_folder", ""))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, project)
            self.list_widget.addItem(item)
            visible_projects.append(project)

        if self.projects_root_folder and os.path.isdir(self.projects_root_folder):
            total_scanned = sum(1 for project in self.all_projects if project.get("_from_scan"))
            if self._scan_status_message:
                self.status_label.setText(
                    f"{self._scan_status_message} Показано: {len(visible_projects)}."
                )
            else:
                self.status_label.setText(
                    f"Найдено проектов в папке: {total_scanned}. Показано: {len(visible_projects)}."
                )
        else:
            self.status_label.setText(f"Показаны недавние проекты: {len(visible_projects)}.")

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)
        self._update_delete_button_state()

    def choose_projects_root_folder(self):
        start_folder = self.projects_root_folder
        if not start_folder and hasattr(self.settings_manager, "get_project_start_folder"):
            start_folder = self.settings_manager.get_project_start_folder()
        folder = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку с проектами",
            start_folder or os.path.expanduser("~")
        )
        if not folder:
            return

        self.hidden_removed_folder_keys.clear()
        self.projects_root_folder = os.path.normpath(folder)
        self.root_folder_edit.setText(self.projects_root_folder)
        self.settings_manager.save_last_projects_root_folder(self.projects_root_folder)
        self._clear_scan_cache()
        self._refresh_project_list(force_scan=True)

    def clear_projects_root_folder(self):
        self.hidden_removed_folder_keys.clear()
        self.projects_root_folder = ""
        self.root_folder_edit.clear()
        self.settings_manager.save_last_projects_root_folder("")
        self._clear_scan_cache()
        self._refresh_project_list()

    def _remove_history_projects(self, projects):
        removed_folder_keys = {
            self._normalize_output_folder(project.get("output_folder"))
            for project in projects
            if project.get("output_folder")
        }
        if not removed_folder_keys:
            return False

        self.history = [
            project for project in self.history
            if self._normalize_output_folder(project.get("output_folder")) not in removed_folder_keys
        ]
        self.hidden_removed_folder_keys.update(removed_folder_keys)
        self.settings_manager.save_project_history(self.history)
        self._refresh_project_list()
        return True

    def _update_delete_button_state(self):
        selected_items = self.list_widget.selectedItems()
        self.delete_button.setEnabled(any(
            item.data(QtCore.Qt.ItemDataRole.UserRole).get("_from_history")
            for item in selected_items
        ))

    def delete_selected(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            return

        history_items = [
            item for item in selected_items
            if item.data(QtCore.Qt.ItemDataRole.UserRole).get("_from_history")
        ]
        if not history_items:
            QMessageBox.information(
                self,
                "Нечего удалять",
                "Можно удалять только записи из недавней истории."
            )
            return

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Подтверждение удаления")
        msg_box.setText(f"Вы уверены, что хотите удалить {len(history_items)} проект(ов) из истории?")
        msg_box.setIcon(QMessageBox.Icon.Question)

        yes_button = msg_box.addButton("Да, удалить", QMessageBox.ButtonRole.YesRole)
        no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(no_button)
        msg_box.exec()

        if msg_box.clickedButton() == yes_button:
            projects_to_remove = [
                item.data(QtCore.Qt.ItemDataRole.UserRole)
                for item in history_items
            ]
            self._remove_history_projects(projects_to_remove)

    def accept(self):
        selected_items = self.list_widget.selectedItems()
        if selected_items:
            self.selected_project = dict(selected_items[0].data(QtCore.Qt.ItemDataRole.UserRole))
            super().accept()
        else:
            QMessageBox.information(self, "Нет выбора", "Пожалуйста, выберите проект из списка.")

    def get_selected_project(self):
        return self.selected_project

    def showEvent(self, event):
        super().showEvent(event)
        if self.list_widget.count() == 0:
            QtCore.QTimer.singleShot(0, self._refresh_project_list)


ProjectHistoryDialog = EnhancedProjectHistoryDialog


class ProjectFolderDialog(QDialog):
    """
    Кастомный диалог для настройки папки проекта с опцией копирования
    исходного файла.
    """
    def __init__(self, parent, main_text, subfolder_path_text):
        super().__init__(parent)
        self.setWindowTitle("Настройка папки проекта")
        self.setMinimumWidth(500)

        self.choice = None  # 'subfolder', 'current' или None для отмены
        self.copy_file_checked = False

        main_layout = QVBoxLayout(self)
        
        # Верхняя часть с иконкой и текстом
        top_panel = QHBoxLayout()
        icon_label = QLabel()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
        icon_label.setPixmap(icon.pixmap(32, 32))
        top_panel.addWidget(icon_label, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        
        text_widget = QLabel(main_text)
        text_widget.setWordWrap(True)
        top_panel.addWidget(text_widget, 1)
        main_layout.addLayout(top_panel)
        
        # Центральная часть с чекбоксом
        self.copy_file_checkbox = QCheckBox(f"Переместить оригинал в папку проекта") # <-- ИЗМЕНЯЕМ ТЕКСТ
        self.copy_file_checkbox.setToolTip(
            "Рекомендуется для полной автономности и портативности проекта.\n"
            "Исходный файл будет перемещен, а не скопирован." # <-- ИЗМЕНЯЕМ ПОДСКАЗКУ
        )
        self.copy_file_checkbox.setChecked(True)
        main_layout.addWidget(self.copy_file_checkbox, 0, QtCore.Qt.AlignmentFlag.AlignCenter)

        # Нижняя часть с кнопками
        button_box = QDialogButtonBox()
        self.btn_create_subfolder = button_box.addButton(f"Создать подпапку '{subfolder_path_text}'", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_use_current = button_box.addButton("Использовать текущую", QDialogButtonBox.ButtonRole.ActionRole)
        cancel_btn = button_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText("Отмена") # <-- Явно задаем русский текст для кнопки
        
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        # Подключаем кастомную кнопку к слоту
        self.btn_use_current.clicked.connect(self._on_use_current)

        main_layout.addWidget(button_box)

    def _on_accept(self):
        self.choice = 'subfolder'
        self.copy_file_checked = self.copy_file_checkbox.isChecked()
        self.accept()

    def _on_use_current(self):
        self.choice = 'current'
        self.copy_file_checked = self.copy_file_checkbox.isChecked()
        self.accept()
        
        
class GeoBlockDialog(QDialog):
    """
    Специализированный, терапевтический диалог для сообщения о геоблокировке,
    который предлагает пользователю решение.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Доступ к API ограничен")
        
        main_layout = QVBoxLayout(self)
        
        # Создаем контейнер для иконки и текста
        content_layout = QHBoxLayout()
        main_layout.addLayout(content_layout)

        # Добавляем стандартную иконку "Предупреждение"
        icon_label = QLabel()
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        icon_label.setPixmap(icon.pixmap(48, 48))
        content_layout.addWidget(icon_label)
        
        # Добавляем текст
        text_layout = QVBoxLayout()
        
        header_label = QLabel("<b>API заблокировал запросы из вашего региона.</b>")
        text_layout.addWidget(header_label)
        
        info_label = QLabel(
            "Похоже, ваш текущий IP-адрес не входит в список избранных у провайдера API.\n\n"
            "Для обхода подобных ограничений в программе предусмотрена настройка прокси. "
            "Вы можете найти кнопку 'Прокси' в самом первом окне при запуске приложения.\n\n"
            "Текущая сессия перевода будет остановлена."
        )
        info_label.setWordWrap(True)
        text_layout.addWidget(info_label)
        
        content_layout.addLayout(text_layout)
        # Добавляем кнопку "ОК"
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Понятно")
        buttons.accepted.connect(self.accept)
        main_layout.addWidget(buttons)
