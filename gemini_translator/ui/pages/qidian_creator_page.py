# -*- coding: utf-8 -*-
"""QidianCreatorPage — Qidian/Fanqie/Ciweimao/Qimao → Rulate creator as an embeddable ShellPage."""

from __future__ import annotations

from pathlib import Path

from PyQt6 import QtWidgets, sip
from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qidian_rulate.models import (
    DEFAULT_RULATE_TELEGRAM_LINK,
    DEFAULT_RULATE_VK_LINK,
    PreparedRulateMetadata,
    QidianBookMetadata,
    RulateBookDraft,
)
from qidian_rulate.workers import (
    AiPrepareWorker,
    CodexCoverGenerateWorker,
    CodexCoverTranslateWorker,
    CoverPromptWorker,
    QidianFetchWorker,
    RulateFillWorker,
    RulateLoginWorker,
    _download_cover_image,
    normalize_rulate_tags,
    validate_source_url,
)

from ..widgets.key_management_widget import KeyManagementWidget
from ..widgets.model_settings_widget import ModelSettingsWidget
from gemini_translator.ui.shell import ShellPage
from gemini_translator.ui.dialogs.qidian_rulate_creator import _split_csv
from ..widgets.overlay_tab_widget import install_tab_fade


QIDIAN_CREATOR_UI_STATE_KEY = "qidian_creator_ui"
CODEX_COVER_DROP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
SOURCE_COVER_DROP_EXTENSIONS = CODEX_COVER_DROP_EXTENSIONS | {".webp"}
CODEX_COVER_DROP_TOOLTIP = "Перетащите PNG, JPG или GIF сюда, чтобы выбрать обложку для Rulate."
SOURCE_COVER_DROP_TOOLTIP = "Перетащите PNG, JPG, GIF или WEBP сюда, если обложка источника не загрузилась."


def _qt_object_is_alive(obj) -> bool:
    if obj is None:
        return False
    try:
        return not sip.isdeleted(obj)
    except TypeError:
        return True


class _CoverDropLabel(QLabel):
    file_dropped = pyqtSignal(str)

    def __init__(
        self,
        text: str = "",
        parent=None,
        *,
        tooltip: str = CODEX_COVER_DROP_TOOLTIP,
        extensions: set[str] | None = None,
    ):
        super().__init__(text, parent)
        self._drop_extensions = extensions or CODEX_COVER_DROP_EXTENSIONS
        self.setAcceptDrops(True)
        self.setToolTip(tooltip)

    def _local_image_path_from_event(self, event) -> str:
        mime_data = event.mimeData()
        if not mime_data or not mime_data.hasUrls():
            return ""
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            image_path = Path(url.toLocalFile())
            if image_path.is_file() and image_path.suffix.lower() in self._drop_extensions:
                return str(image_path)
        return ""

    def dragEnterEvent(self, event) -> None:
        if self._local_image_path_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._local_image_path_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        image_path = self._local_image_path_from_event(event)
        if not image_path:
            event.ignore()
            return
        self.file_dropped.emit(image_path)
        event.acceptProposedAction()


class QidianCreatorPage(ShellPage):
    page_title = "Qidian/Fanqie/Ciweimao/Qimao → Rulate"

    def __init__(self, parent=None):
        super().__init__(parent)

        app = QtWidgets.QApplication.instance()
        if not app or not hasattr(app, "get_settings_manager"):
            raise RuntimeError("SettingsManager не найден в QApplication.")

        self.settings_manager = app.get_settings_manager()
        self.server_manager = getattr(app, "server_manager", None)
        self._qidian_metadata: QidianBookMetadata | None = None
        self._prepared_metadata: PreparedRulateMetadata | None = None
        self._prepare_ai_worker: AiPrepareWorker | None = None
        self._prepare_ai_cancel_requested = False
        self._local_source_cover_path = ""
        self._source_cover_image_data = b""
        self._generated_cover_path = ""
        self._workers = []

        self._build_ui()
        self._load_ui_state()
        self._connect_ai_widgets()
        self._update_action_state()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self.main_tabs = QTabWidget()
        install_tab_fade(self.main_tabs)
        self.main_tabs.setDocumentMode(False)
        root.addWidget(self.main_tabs, 1)

        main_tab = QWidget()
        main_layout = QVBoxLayout(main_tab)

        main_layout.addWidget(self._build_source_group())

        unified_scroll = QScrollArea()
        unified_scroll.setWidgetResizable(True)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        scroll_layout.addWidget(self._build_preview_group(), 1)

        self.key_widget = KeyManagementWidget(
            self.settings_manager,
            self,
            server_manager=self.server_manager,
        )
        self.model_settings_widget = ModelSettingsWidget(
            self,
            settings_manager=self.settings_manager,
            server_manager=self.server_manager,
        )
        self.model_settings_widget.set_cjk_options_visible(False)
        self.model_settings_widget.set_glossary_options_visible(False)
        self.model_settings_widget.set_misc_options_visible(False)

        scroll_layout.addWidget(self.key_widget)
        scroll_layout.addWidget(self.model_settings_widget)

        unified_scroll.setWidget(scroll_content)
        main_layout.addWidget(unified_scroll, 1)

        self.main_tabs.addTab(main_tab, "Основное")

        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log_edit)
        self.main_tabs.addTab(log_tab, "Лог")

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("Источник и действия")
        layout = QVBoxLayout(group)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("URL источника:"))
        self.qidian_url_edit = QLineEdit("https://www.qidian.com/book/1041604040/")
        self.qidian_url_edit.setPlaceholderText(
            "Qidian, Fanqie, Ciweimao или https://www.qimao.com/shuku/195958/"
        )
        url_row.addWidget(self.qidian_url_edit, 1)
        self.visible_qidian_checkbox = QCheckBox("Открывать источник видимо")
        url_row.addWidget(self.visible_qidian_checkbox)
        layout.addLayout(url_row)

        action_row = QHBoxLayout()
        self.fetch_qidian_btn = QPushButton("Получить данные источника")
        self.fetch_qidian_btn.clicked.connect(self._fetch_qidian)
        action_row.addWidget(self.fetch_qidian_btn)

        self.prepare_ai_btn = QPushButton("Подготовить перевод, жанры, теги и промпт")
        self.prepare_ai_btn.clicked.connect(self._prepare_ai)
        action_row.addWidget(self.prepare_ai_btn)

        self.cancel_prepare_ai_btn = QPushButton("Отменить генерацию")
        self.cancel_prepare_ai_btn.clicked.connect(self._cancel_prepare_ai)
        self.cancel_prepare_ai_btn.setVisible(False)
        action_row.addWidget(self.cancel_prepare_ai_btn)

        self.cover_prompt_btn = QPushButton("Сгенерировать промпт для обложки")
        self.cover_prompt_btn.clicked.connect(self._generate_cover_prompt)
        action_row.addWidget(self.cover_prompt_btn)

        self.login_rulate_btn = QPushButton("Войти в Rulate")
        self.login_rulate_btn.clicked.connect(self._login_rulate)
        action_row.addWidget(self.login_rulate_btn)

        self.fill_rulate_btn = QPushButton("Открыть и заполнить Rulate")
        self.fill_rulate_btn.clicked.connect(self._fill_rulate)
        action_row.addWidget(self.fill_rulate_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        hint = QLabel(
            "Форма Rulate заполняется в открытом браузере. Проверьте поля и сохраните вручную."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        return group

    def _build_preview_group(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_qidian_group())
        splitter.addWidget(self._build_rulate_group())
        splitter.setSizes([560, 560])
        return splitter

    def _build_qidian_group(self) -> QGroupBox:
        group = QGroupBox("Данные источника")
        layout = QFormLayout(group)

        self.original_title_edit = QLineEdit()
        self.author_edit = QLineEdit()
        self.source_url_edit = QLineEdit()
        self.cover_url_edit = QLineEdit()
        self.cover_url_edit.editingFinished.connect(self._load_cover_preview_from_current_url)
        self.reload_cover_btn = QPushButton("Загрузить")
        self.reload_cover_btn.clicked.connect(self._load_cover_preview_from_current_url)
        cover_url_widget = QWidget()
        cover_url_layout = QHBoxLayout(cover_url_widget)
        cover_url_layout.setContentsMargins(0, 0, 0, 0)
        cover_url_layout.addWidget(self.cover_url_edit, 1)
        cover_url_layout.addWidget(self.reload_cover_btn)

        self.cover_preview_label = _CoverDropLabel(
            "Обложка не загружена",
            tooltip=SOURCE_COVER_DROP_TOOLTIP,
            extensions=SOURCE_COVER_DROP_EXTENSIONS,
        )
        self.cover_preview_label.file_dropped.connect(self._apply_dropped_source_cover)
        self.cover_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview_label.setFixedSize(150, 210)
        self.cover_preview_label.setStyleSheet(
            "QLabel { border: 1px solid #444; background: #15191d; color: #888; }"
        )
        self.description_edit = QTextEdit()
        self.description_edit.setAcceptRichText(False)
        self.description_edit.setMinimumHeight(170)

        layout.addRow("Название:", self.original_title_edit)
        layout.addRow("Автор:", self.author_edit)
        layout.addRow("Оригинал:", self.source_url_edit)
        layout.addRow("Обложка URL:", cover_url_widget)
        layout.addRow("Превью:", self.cover_preview_label)
        layout.addRow("Описание:", self.description_edit)

        return group

    def _build_rulate_group(self) -> QGroupBox:
        group = QGroupBox("Черновик Rulate")
        layout = QFormLayout(group)

        self.english_title_edit = QLineEdit()
        self.translated_title_edit = QLineEdit()
        self.translated_description_edit = QTextEdit()
        self.translated_description_edit.setAcceptRichText(False)
        self.translated_description_edit.setMinimumHeight(170)
        self.translator_team_combo = QComboBox()
        self.translator_team_combo.addItem("Первая подсказка", "first_suggestion")
        self.translator_team_combo.addItem("Не выбирать", "")
        self.translator_team_combo.setToolTip(
            "Автоматически выбирает первую команду из подсказок Rulate, без ручного ввода ID или названия."
        )
        self.vk_link_edit = QLineEdit(DEFAULT_RULATE_VK_LINK)
        self.vk_link_edit.setPlaceholderText("Ссылка на группу VK")
        self.telegram_link_edit = QLineEdit(DEFAULT_RULATE_TELEGRAM_LINK)
        self.telegram_link_edit.setPlaceholderText("Ссылка на Telegram-канал")
        self.genres_edit = QLineEdit()
        self.genres_edit.setPlaceholderText("фэнтези, мистика, приключения")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("китайская новелла, тайны, сверхъестественное")
        self.cover_prompt_edit = QTextEdit()
        self.cover_prompt_edit.setAcceptRichText(False)
        self.cover_prompt_edit.setMinimumHeight(130)
        self.cover_prompt_edit.setPlaceholderText("Здесь появится английский промпт для генерации обложки")
        self.codex_cover_btn = QPushButton("Сгенерировать в Codex")
        self.codex_cover_btn.setToolTip("Запустить Codex с текущим промптом обложки и показать результат здесь.")
        self.codex_cover_btn.clicked.connect(self._generate_cover_in_codex)
        self.translate_cover_btn = QPushButton("Перевести обложку")
        self.translate_cover_btn.setToolTip(
            "Удалить текст и рекламу с исходной обложки, поставить русское название и запросить 2:3/4K-качество."
        )
        self.translate_cover_btn.clicked.connect(self._translate_cover_in_codex)
        self.open_codex_cover_folder_btn = QPushButton("Открыть папку")
        self.open_codex_cover_folder_btn.setToolTip("Открыть папку с Codex-обложкой.")
        self.open_codex_cover_folder_btn.clicked.connect(self._open_codex_cover_folder)
        self.open_codex_cover_folder_btn.setEnabled(False)
        self.codex_cover_preview_label = _CoverDropLabel("Обложка\nне создана")
        self.codex_cover_preview_label.file_dropped.connect(self._apply_dropped_codex_cover)
        self.codex_cover_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.codex_cover_preview_label.setFixedSize(150, 210)
        self.codex_cover_preview_label.setStyleSheet(
            "QLabel { border: 1px solid #444; background: #15191d; color: #888; }"
        )
        self.codex_cover_path_label = QLabel("")
        self.codex_cover_path_label.setMaximumHeight(34)
        self.codex_cover_path_label.setWordWrap(True)
        self.codex_cover_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        cover_generation_widget = QWidget()
        cover_generation_layout = QHBoxLayout(cover_generation_widget)
        cover_generation_layout.setContentsMargins(0, 0, 0, 0)
        cover_generation_layout.setSpacing(8)
        cover_generation_controls = QWidget()
        cover_generation_controls.setFixedWidth(170)
        cover_generation_controls_layout = QVBoxLayout(cover_generation_controls)
        cover_generation_controls_layout.setContentsMargins(0, 0, 0, 0)
        cover_generation_controls_layout.setSpacing(6)
        cover_generation_controls_layout.addWidget(self.codex_cover_btn)
        cover_generation_controls_layout.addWidget(self.translate_cover_btn)
        cover_generation_controls_layout.addWidget(self.open_codex_cover_folder_btn)
        cover_generation_controls_layout.addWidget(
            self.codex_cover_preview_label,
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        cover_generation_controls_layout.addWidget(self.codex_cover_path_label)
        cover_generation_controls_layout.addStretch()
        cover_generation_layout.addWidget(cover_generation_controls)
        cover_generation_layout.addWidget(self.cover_prompt_edit, 1)

        layout.addRow("Название EN:", self.english_title_edit)
        layout.addRow("Название RU:", self.translated_title_edit)
        layout.addRow("Описание RU:", self.translated_description_edit)
        layout.addRow("Команда переводчиков:", self.translator_team_combo)
        layout.addRow("VK:", self.vk_link_edit)
        layout.addRow("Telegram:", self.telegram_link_edit)
        layout.addRow("Жанры:", self.genres_edit)
        layout.addRow("Теги:", self.tags_edit)
        layout.addRow("Промпт обложки:", cover_generation_widget)

        return group

    def _connect_ai_widgets(self) -> None:
        provider_id = self.key_widget.get_selected_provider()
        self.model_settings_widget.set_available_models(provider_id)
        self.key_widget.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.model_settings_widget.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.translator_team_combo.currentIndexChanged.connect(self._on_translator_team_mode_changed)
        self._on_model_changed(self.model_settings_widget.model_combo.currentIndex())

    def _on_provider_changed(self, _index: int) -> None:
        provider_id = self.key_widget.get_selected_provider()
        self.model_settings_widget.set_available_models(provider_id)
        self._on_model_changed(self.model_settings_widget.model_combo.currentIndex())

    def _on_model_changed(self, index: int) -> None:
        if index < 0:
            return
        model_id = self.model_settings_widget.model_combo.itemData(index)
        if model_id:
            self.key_widget.set_current_model(model_id)

    def _fetch_qidian(self) -> None:
        url = self.qidian_url_edit.text().strip()
        if not validate_source_url(url):
            QMessageBox.warning(
                self,
                "Источник",
                "Введите ссылку вида https://www.qidian.com/book/1041604040/ "
                "или https://fanqienovel.com/page/7229603492648717324 "
                "или https://www.ciweimao.com/book/100441110 "
                "или https://www.qimao.com/shuku/195958/",
            )
            return
        self.fetch_qidian_btn.setEnabled(False)
        worker = QidianFetchWorker(url, visible_browser=self.visible_qidian_checkbox.isChecked())
        worker.log_signal.connect(self._log)
        worker.metadata_ready.connect(self._apply_qidian_metadata)
        worker.finished_signal.connect(lambda: self._worker_finished(worker, self.fetch_qidian_btn))
        self._workers.append(worker)
        worker.start()

    def _prepare_ai(self) -> None:
        metadata = self._collect_qidian_metadata()
        if not metadata.title_original or not metadata.description:
            QMessageBox.warning(self, "AI", "Сначала получите или заполните название и описание источника.")
            return

        provider_id = self.key_widget.get_selected_provider()
        active_keys = self.key_widget.get_active_keys()
        model_settings = self.model_settings_widget.get_settings()
        worker = AiPrepareWorker(
            metadata,
            provider_id,
            model_settings,
            active_keys,
            self.settings_manager,
            visible_browser=self.visible_qidian_checkbox.isChecked(),
        )
        worker.log_signal.connect(self._log)
        worker.prepared_ready.connect(self._apply_prepared_metadata)
        worker.finished_signal.connect(lambda: self._prepare_ai_worker_finished(worker))
        self._prepare_ai_worker = worker
        self._prepare_ai_cancel_requested = False
        self._set_prepare_ai_running(True)
        self._workers.append(worker)
        worker.start()

    def _cancel_prepare_ai(self) -> None:
        worker = getattr(self, "_prepare_ai_worker", None)
        if worker is None:
            return
        cancel_method = getattr(worker, "cancel", None)
        if callable(cancel_method):
            cancel_method()
        self._prepare_ai_cancel_requested = True
        self._set_prepare_ai_running(True)
        self._log("INFO", "AI: запрошена отмена генерации.")

    def _generate_cover_prompt(self) -> None:
        url = self.source_url_edit.text().strip() or self.qidian_url_edit.text().strip()
        if not validate_source_url(url):
            QMessageBox.warning(
                self,
                "Обложка",
                "Введите ссылку вида https://www.qidian.com/book/1041604040/ "
                "или https://fanqienovel.com/page/7229603492648717324 "
                "или https://www.ciweimao.com/book/100441110 "
                "или https://www.qimao.com/shuku/195958/",
            )
            return

        title_ru = self.translated_title_edit.text().strip()
        if not title_ru:
            QMessageBox.warning(self, "Обложка", "Сначала заполните русское название.")
            return

        provider_id = self.key_widget.get_selected_provider()
        active_keys = self.key_widget.get_active_keys()
        model_settings = self.model_settings_widget.get_settings()
        self.cover_prompt_btn.setEnabled(False)
        worker = CoverPromptWorker(
            url,
            title_ru,
            provider_id,
            model_settings,
            active_keys,
            self.settings_manager,
            original_description=self.description_edit.toPlainText().strip(),
            visible_browser=self.visible_qidian_checkbox.isChecked(),
        )
        worker.log_signal.connect(self._log)
        worker.prompt_ready.connect(self._apply_cover_prompt)
        worker.finished_signal.connect(lambda: self._worker_finished(worker, self.cover_prompt_btn))
        self._workers.append(worker)
        worker.start()

    def _generate_cover_in_codex(self) -> None:
        cover_prompt = self.cover_prompt_edit.toPlainText().strip()
        if not cover_prompt:
            QMessageBox.warning(self, "Codex", "Сначала заполните промпт обложки.")
            return

        self._set_codex_cover_buttons_enabled(False)
        self._generated_cover_path = ""
        self._set_codex_cover_folder_button_enabled(False)
        self._set_codex_cover_preview(None, "Генерация...")
        worker = CodexCoverGenerateWorker(
            cover_prompt,
            title_ru=self.translated_title_edit.text().strip(),
        )
        worker.log_signal.connect(self._log)
        worker.cover_ready.connect(self._apply_codex_cover)
        worker.finished_signal.connect(lambda: self._codex_cover_worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def _translate_cover_in_codex(self) -> None:
        cover_url = self.cover_url_edit.text().strip()
        source_image_path = getattr(self, "_local_source_cover_path", "")
        source_image_data = getattr(self, "_source_cover_image_data", b"")
        if not cover_url and not source_image_path and not source_image_data:
            QMessageBox.warning(
                self,
                "Codex",
                "Сначала загрузите/укажите URL исходной обложки или перетащите локальную обложку в левое превью.",
            )
            return

        title_ru = self.translated_title_edit.text().strip()
        if not title_ru:
            QMessageBox.warning(self, "Codex", "Сначала заполните русское название.")
            return

        self._set_codex_cover_buttons_enabled(False)
        self._generated_cover_path = ""
        self._set_codex_cover_folder_button_enabled(False)
        self._set_codex_cover_preview(None, "Перевод...")
        worker = CodexCoverTranslateWorker(
            cover_url,
            title_ru,
            referer=self.source_url_edit.text().strip() or self.qidian_url_edit.text().strip(),
            source_image_path=source_image_path,
            source_image_data=source_image_data,
        )
        worker.log_signal.connect(self._log)
        worker.cover_ready.connect(self._apply_codex_cover)
        worker.finished_signal.connect(lambda: self._codex_cover_worker_finished(worker))
        self._workers.append(worker)
        worker.start()

    def _login_rulate(self) -> None:
        self.login_rulate_btn.setEnabled(False)
        worker = RulateLoginWorker()
        worker.log_signal.connect(self._log)
        worker.finished_signal.connect(lambda: self._worker_finished(worker, self.login_rulate_btn))
        self._workers.append(worker)
        worker.start()

    def _fill_rulate(self) -> None:
        self._save_ui_state()
        qidian = self._collect_qidian_metadata()
        prepared = self._collect_prepared_metadata()
        try:
            prepared.tags = normalize_rulate_tags(prepared.tags)
            self.tags_edit.setText(", ".join(prepared.tags))
        except ValueError as error:
            QMessageBox.warning(self, "Rulate", str(error))
            return

        missing = []
        if not qidian.title_original:
            missing.append("китайское название")
        if not qidian.author_name:
            missing.append("автор")
        if not qidian.source_url:
            missing.append("ссылка на оригинал")
        if not prepared.english_title:
            missing.append("английское название")
        if not prepared.translated_title:
            missing.append("название на языке перевода")
        if not prepared.translated_description:
            missing.append("описание")
        if len(prepared.genres) < 3:
            missing.append("минимум 3 жанра")
        if len(prepared.tags) < 3:
            missing.append("минимум 3 тега")
        if missing:
            QMessageBox.warning(self, "Rulate", "Не хватает данных: " + ", ".join(missing))
            return

        self.fill_rulate_btn.setEnabled(False)
        worker = RulateFillWorker(RulateBookDraft(qidian=qidian, prepared=prepared))
        worker.log_signal.connect(self._log)
        worker.finished_signal.connect(lambda: self._worker_finished(worker, self.fill_rulate_btn))
        self._workers.append(worker)
        worker.start()

    def _apply_qidian_metadata(self, metadata: QidianBookMetadata) -> None:
        if not _qt_object_is_alive(self):
            return
        self._qidian_metadata = metadata
        self._local_source_cover_path = ""
        self.original_title_edit.setText(metadata.title_original)
        self.author_edit.setText(metadata.author_name)
        self.source_url_edit.setText(metadata.source_url)
        self.cover_url_edit.setText(metadata.cover_url)
        self._set_cover_preview(metadata.cover_image_data)
        if metadata.cover_url and not metadata.cover_image_data:
            self._load_cover_preview_from_current_url()
        self.description_edit.setPlainText(metadata.description)
        self._update_action_state()

    def _apply_prepared_metadata(self, prepared: PreparedRulateMetadata) -> None:
        if not _qt_object_is_alive(self):
            return
        self._prepared_metadata = prepared
        self.english_title_edit.setText(prepared.english_title)
        self.translated_title_edit.setText(prepared.translated_title)
        self.translated_description_edit.setPlainText(prepared.translated_description)
        self.genres_edit.setText(", ".join(prepared.genres))
        self.tags_edit.setText(", ".join(prepared.tags))
        translator_team_mode = getattr(prepared, "translator_team_mode", "")
        if translator_team_mode:
            index = self.translator_team_combo.findData(translator_team_mode)
            if index >= 0:
                self.translator_team_combo.setCurrentIndex(index)
        vk_link = getattr(prepared, "vk_link", "") or ""
        if vk_link:
            self.vk_link_edit.setText(vk_link)
        telegram_link = getattr(prepared, "telegram_link", "") or ""
        if telegram_link:
            self.telegram_link_edit.setText(telegram_link)
        if prepared.cover_prompt:
            self.cover_prompt_edit.setPlainText(prepared.cover_prompt)
        generated_cover_path = getattr(prepared, "generated_cover_path", "") or ""
        if generated_cover_path:
            self._generated_cover_path = generated_cover_path
            self._set_codex_cover_preview(generated_cover_path)
        self._update_action_state()

    def _apply_cover_prompt(self, prompt: str) -> None:
        if not _qt_object_is_alive(self):
            return
        self.cover_prompt_edit.setPlainText(prompt)
        self._update_action_state()

    def _apply_codex_cover(self, image_path: str) -> None:
        if not _qt_object_is_alive(self):
            return
        self._generated_cover_path = image_path
        self._set_codex_cover_preview(image_path)
        self._update_action_state()

    def _apply_dropped_source_cover(self, image_path: str) -> None:
        if not _qt_object_is_alive(self):
            return
        cover_path = Path(image_path).expanduser()
        if not cover_path.is_file():
            QMessageBox.warning(self, "Источник", "Файл обложки не найден.")
            return
        if cover_path.suffix.lower() not in SOURCE_COVER_DROP_EXTENSIONS:
            QMessageBox.warning(self, "Источник", "Для исходной обложки выберите PNG, JPG, GIF или WEBP.")
            return

        try:
            image_data = cover_path.read_bytes()
        except OSError as error:
            QMessageBox.warning(self, "Источник", f"Не удалось открыть файл обложки: {error}")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_data):
            QMessageBox.warning(self, "Источник", "Не удалось прочитать изображение.")
            return

        self._local_source_cover_path = str(cover_path.resolve())
        self._set_cover_preview(image_data)
        if _qt_object_is_alive(getattr(self, "cover_preview_label", None)):
            self.cover_preview_label.setToolTip(self._local_source_cover_path)
        self._log("INFO", f"Источник: выбрана локальная обложка для Codex: {self._local_source_cover_path}")

    def _apply_dropped_codex_cover(self, image_path: str) -> None:
        if not _qt_object_is_alive(self):
            return
        cover_path = Path(image_path).expanduser()
        if not cover_path.is_file():
            QMessageBox.warning(self, "Codex", "Файл обложки не найден.")
            return
        if cover_path.suffix.lower() not in CODEX_COVER_DROP_EXTENSIONS:
            QMessageBox.warning(self, "Codex", "Для загрузки в Rulate выберите PNG, JPG или GIF.")
            return

        pixmap = QPixmap()
        if not pixmap.load(str(cover_path)):
            QMessageBox.warning(self, "Codex", "Не удалось прочитать изображение.")
            return

        self._generated_cover_path = str(cover_path.resolve())
        self._set_codex_cover_preview(self._generated_cover_path)
        self._update_action_state()
        self._log("INFO", f"Codex: выбрана локальная обложка для Rulate: {self._generated_cover_path}")

    def _open_codex_cover_folder(self) -> None:
        if not _qt_object_is_alive(self):
            return
        cover_path = Path(getattr(self, "_generated_cover_path", "") or "").expanduser()
        if not cover_path.is_file():
            self._set_codex_cover_folder_button_enabled(False)
            QMessageBox.warning(self, "Codex", "Сначала создайте или переведите обложку в Codex.")
            return

        folder_path = cover_path.parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_path))):
            QMessageBox.warning(self, "Codex", f"Не удалось открыть папку:\n{folder_path}")

    def _load_ui_state(self) -> None:
        saved = {}
        try:
            saved = self.settings_manager.load_settings().get(QIDIAN_CREATOR_UI_STATE_KEY, {}) or {}
        except Exception:
            saved = {}

        if not isinstance(saved, dict):
            return

        if "translator_team_mode" in saved:
            index = self.translator_team_combo.findData(saved.get("translator_team_mode") or "")
            if index >= 0:
                self.translator_team_combo.setCurrentIndex(index)
        if saved.get("vk_link"):
            self.vk_link_edit.setText(str(saved["vk_link"]))
        if saved.get("telegram_link"):
            self.telegram_link_edit.setText(str(saved["telegram_link"]))

    def _save_ui_state(self) -> None:
        try:
            self.settings_manager.save_ui_state(
                {
                    QIDIAN_CREATOR_UI_STATE_KEY: {
                        "translator_team_mode": self.translator_team_combo.currentData() or "",
                        "vk_link": self.vk_link_edit.text().strip(),
                        "telegram_link": self.telegram_link_edit.text().strip(),
                    }
                }
            )
        except Exception:
            pass

    def _on_translator_team_mode_changed(self, _index: int) -> None:
        self._save_ui_state()

    def on_leave(self) -> None:
        self._save_ui_state()

    def _collect_qidian_metadata(self) -> QidianBookMetadata:
        return QidianBookMetadata(
            source_url=self.source_url_edit.text().strip() or self.qidian_url_edit.text().strip(),
            title_original=self.original_title_edit.text().strip(),
            author_name=self.author_edit.text().strip(),
            description=self.description_edit.toPlainText().strip(),
            cover_url=self.cover_url_edit.text().strip(),
        )

    def _collect_prepared_metadata(self) -> PreparedRulateMetadata:
        return PreparedRulateMetadata(
            english_title=self.english_title_edit.text().strip(),
            translated_title=self.translated_title_edit.text().strip(),
            translated_description=self.translated_description_edit.toPlainText().strip(),
            translator_team_mode=self.translator_team_combo.currentData() or "",
            vk_link=self.vk_link_edit.text().strip(),
            telegram_link=self.telegram_link_edit.text().strip(),
            genres=_split_csv(self.genres_edit.text()),
            tags=_split_csv(self.tags_edit.text()),
            cover_prompt=self.cover_prompt_edit.toPlainText().strip(),
            generated_cover_path=getattr(self, "_generated_cover_path", ""),
        )

    def _update_action_state(self) -> None:
        if not _qt_object_is_alive(self):
            return
        self._set_prepare_ai_running(getattr(self, "_prepare_ai_worker", None) is not None)
        self._set_button_enabled(self.login_rulate_btn, True)
        self._set_button_enabled(self.fill_rulate_btn, True)
        folder_button_updater = getattr(self, "_set_codex_cover_folder_button_enabled", None)
        if callable(folder_button_updater):
            folder_button_updater()

    def _set_prepare_ai_running(self, running: bool) -> None:
        prepare_btn = getattr(self, "prepare_ai_btn", None)
        if _qt_object_is_alive(prepare_btn):
            prepare_btn.setVisible(not running)
            prepare_btn.setEnabled(not running)

        cancel_btn = getattr(self, "cancel_prepare_ai_btn", None)
        if _qt_object_is_alive(cancel_btn):
            cancel_requested = bool(getattr(self, "_prepare_ai_cancel_requested", False))
            cancel_btn.setText("Отмена..." if running and cancel_requested else "Отменить генерацию")
            cancel_btn.setVisible(running)
            cancel_btn.setEnabled(running and not cancel_requested)

    def _set_button_enabled(self, button: QPushButton | None, enabled: bool) -> None:
        if _qt_object_is_alive(button):
            button.setEnabled(enabled)

    def _worker_finished(self, worker, button: QPushButton) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if not _qt_object_is_alive(self):
            return
        self._set_button_enabled(button, True)
        self._update_action_state()

    def _prepare_ai_worker_finished(self, worker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if getattr(self, "_prepare_ai_worker", None) is worker:
            self._prepare_ai_worker = None
        self._prepare_ai_cancel_requested = False
        if not _qt_object_is_alive(self):
            return
        self._set_prepare_ai_running(False)
        self._update_action_state()

    def _codex_cover_worker_finished(self, worker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if not _qt_object_is_alive(self):
            return
        self._set_codex_cover_buttons_enabled(True)
        self._update_action_state()

    def _set_codex_cover_buttons_enabled(self, enabled: bool) -> None:
        self._set_button_enabled(getattr(self, "codex_cover_btn", None), enabled)
        self._set_button_enabled(getattr(self, "translate_cover_btn", None), enabled)

    def _set_codex_cover_folder_button_enabled(self, enabled: bool | None = None) -> None:
        if enabled is None:
            cover_path = Path(getattr(self, "_generated_cover_path", "") or "")
            enabled = bool(getattr(self, "_generated_cover_path", "")) and cover_path.is_file()
        self._set_button_enabled(getattr(self, "open_codex_cover_folder_btn", None), enabled)

    def _log(self, level: str, message: str) -> None:
        if level == "DEBUG" and not message:
            return
        if not _qt_object_is_alive(self):
            return
        log_edit = getattr(self, "log_edit", None)
        if not _qt_object_is_alive(log_edit):
            return
        log_edit.appendPlainText(f"[{level}] {message}")
        log_edit.verticalScrollBar().setValue(log_edit.verticalScrollBar().maximum())

    def _set_cover_preview(self, image_data: bytes) -> None:
        pixmap = QPixmap()
        if image_data and pixmap.loadFromData(image_data):
            self._source_cover_image_data = bytes(image_data)
            scaled = pixmap.scaled(
                self.cover_preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.cover_preview_label.setPixmap(scaled)
            self.cover_preview_label.setToolTip(SOURCE_COVER_DROP_TOOLTIP)
            return
        self._source_cover_image_data = b""
        self.cover_preview_label.clear()
        self.cover_preview_label.setText("Обложка не загружена")
        self.cover_preview_label.setToolTip(SOURCE_COVER_DROP_TOOLTIP)

    def _load_cover_preview_from_current_url(self) -> None:
        cover_url = self.cover_url_edit.text().strip()
        if not cover_url:
            self._set_cover_preview(b"")
            return
        image_data = _download_cover_image(
            cover_url,
            referer=self.source_url_edit.text().strip() or self.qidian_url_edit.text().strip(),
        )
        if image_data:
            self._local_source_cover_path = ""
        self._set_cover_preview(image_data)

    def _set_codex_cover_preview(self, image_path: str | None, placeholder: str = "Обложка\nне создана") -> None:
        if not _qt_object_is_alive(self):
            return
        label = getattr(self, "codex_cover_preview_label", None)
        if not _qt_object_is_alive(label):
            return

        pixmap = QPixmap()
        if image_path and pixmap.load(image_path):
            scaled = pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
            label.setToolTip(image_path)
            if _qt_object_is_alive(getattr(self, "codex_cover_path_label", None)):
                self.codex_cover_path_label.setText(Path(image_path).name)
                self.codex_cover_path_label.setToolTip(image_path)
            return

        label.clear()
        label.setText(placeholder)
        label.setToolTip(CODEX_COVER_DROP_TOOLTIP)
        if _qt_object_is_alive(getattr(self, "codex_cover_path_label", None)):
            self.codex_cover_path_label.setText("")
            self.codex_cover_path_label.setToolTip("")
