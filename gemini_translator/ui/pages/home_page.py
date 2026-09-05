"""Home page of the navigation shell: the tool picker.

Renders the translator tool cards and emits ``tool_selected(tool_id)``. The
shell decides what each id does and pushes the selected tool page.

Each tool is a flat ``QPushButton`` styled as a card (accent icon tile + title
+ description, hero card adds an "Открыть" pill). Child labels are transparent
to mouse events so the whole card is clickable, and ``tool_buttons[tool_id]``
stays a real button (``.click()`` works).
"""
from __future__ import annotations

from PyQt6 import QtCore, QtWidgets
import os
import sys
from gemini_translator.ui.shell import ShellPage
from gemini_translator.utils import updater as upd

# (icon, title, description, tool_id, is_large)
_TOOLS = [
    ("📖", "Переводчик EPUB",
     "Многопоточный перевод книг через Gemini / OpenRouter / GLM с контролем "
     "промпта, глоссария и пакетных задач.",
     "translator", True),
    ("✅", "Валидатор переводов",
     "Вычитка и доработка: текст и HTML бок о бок.",
     "validator", False),
    ("📚", "Менеджер глоссариев",
     "Редактор терминов: AI или ручной режим.",
     "glossary", False),
    ("📝", "EPUB → Rulate MD",
     "Конвертер EPUB в markdown для Rulate.",
     "rulate_export", False),
    ("✂️", "Сплиттер глав",
     "Разбивает большие главы на части.",
     "chapter_splitter", False),
    ("🎧", "Gemini Reader",
     "Озвучивание EPUB через Gemini Live.",
     "gemini_reader", False),
    ("☁️", "RanobeLib Uploader",
     "Загрузчик глав на RanobeLib.",
     "ranobelib_uploader", False),
    ("✏️", "Qidian/Fanqie/Ciweimao/Qimao → Rulate",
     "Черновик книги: данные Qidian, Fanqie, Ciweimao или Qimao + AI-перевод.",
     "qidian_rulate_creator", False),
    ("📊", "Бенчмарк промптов",
     "Сравнение промптов и моделей.",
     "prompt_benchmark", False),
]

_TRANSPARENT = QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents


class _ToolCard(QtWidgets.QFrame):
    """Clickable card: accent icon tile + title + description (+ hero pill).

    A QFrame (sizes to its layout reliably, unlike a QPushButton with child
    widgets) that emits ``clicked`` on left-release; ``click()`` is provided for
    programmatic/test activation.
    """

    clicked = QtCore.pyqtSignal()

    def __init__(self, icon, title, description, is_large, parent=None):
        super().__init__(parent)
        self.setObjectName("toolHeroCard" if is_large else "toolCard")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(14, 13, 14, 13)
        row.setSpacing(13)

        tile = QtWidgets.QLabel(icon)
        tile.setObjectName("toolIconTile")
        tile.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        size = 46 if is_large else 38
        tile.setFixedSize(size, size)
        tile.setAttribute(_TRANSPARENT, True)
        row.addWidget(tile, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        text_col = QtWidgets.QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)
        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("toolHeroTitle" if is_large else "toolCardTitle")
        title_label.setAttribute(_TRANSPARENT, True)
        text_col.addWidget(title_label)
        detail_label = QtWidgets.QLabel(description)
        detail_label.setObjectName("toolCardDetail")
        detail_label.setWordWrap(True)
        detail_label.setAttribute(_TRANSPARENT, True)
        text_col.addWidget(detail_label)
        row.addLayout(text_col, 1)

        if is_large:
            open_pill = QtWidgets.QLabel("Открыть")
            open_pill.setObjectName("toolOpenPill")
            open_pill.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            open_pill.setAttribute(_TRANSPARENT, True)
            row.addWidget(open_pill, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

    def click(self) -> None:
        """Programmatic activation (used by tests and keyboard)."""
        self.clicked.emit()

    def mouseReleaseEvent(self, event):
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _UpdateAvailableDialog(QtWidgets.QDialog):
    """Overlay-card content for release notes with fixed action buttons."""

    def __init__(
        self,
        title_version: str,
        html_description: str,
        install_label: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Доступно обновление")
        self.action = "later"
        self.setMinimumWidth(420)
        self.resize(640, 520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        heading = QtWidgets.QLabel(
            f"Доступна новая версия: <b>{title_version}</b>", self
        )
        heading.setTextFormat(QtCore.Qt.TextFormat.RichText)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        notes = QtWidgets.QTextBrowser(self)
        notes.setHtml(html_description)
        notes.setOpenExternalLinks(True)
        notes.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        notes.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        layout.addWidget(notes, 1)

        buttons = QtWidgets.QDialogButtonBox(self)
        install = buttons.addButton(
            install_label, QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole
        )
        later = buttons.addButton(
            "Напомнить позже", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole
        )
        ignore = buttons.addButton(
            "Игнорировать", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole
        )
        install.setDefault(True)
        install.clicked.connect(lambda: self._finish("install"))
        later.clicked.connect(lambda: self._finish("later"))
        ignore.clicked.connect(lambda: self._finish("ignore"))
        layout.addWidget(buttons)

    def _finish(self, action: str) -> None:
        self.action = action
        if action == "install":
            self.accept()
        else:
            self.reject()


class HomePage(ShellPage):
    page_title = ""  # home shows no Back; nav bar title stays empty

    tool_selected = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tool_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._update_state = upd.UpdateState.IDLE
        self._update_silent = False
        self._last_silent_error = None
        self._silent_retry_scheduled = False
        self._update_checker = None
        self._update_worker = None
        self._downloader = None
        self._download_progress = None
        self.proxy_status_label = QtWidgets.QLabel("Прокси: выключен")
        self.proxy_status_label.setObjectName("helperLabel")
        self.proxy_status_label.setToolTip("Сетевые запросы идут без прокси.")
        self.proxy_button = QtWidgets.QPushButton("Прокси")
        self.proxy_button.setObjectName("compactActionButton")
        self.proxy_button.clicked.connect(self._open_proxy_settings)
        self._build_ui()
        self._refresh_proxy_status()
        import os
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            QtCore.QTimer.singleShot(1000, lambda: self.check_for_updates(silent=True))

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(16)

        top_row = QtWidgets.QHBoxLayout()
        self.btn_check_update = QtWidgets.QPushButton("Проверить обновления")
        self.btn_check_update.setFixedSize(160, 30)
        self.btn_check_update.clicked.connect(lambda: self.check_for_updates(silent=False))
        top_row.addWidget(self.btn_check_update)
        
        from gemini_translator.version import APP_VERSION
        self.lbl_version = QtWidgets.QLabel(f"Текущая версия: {APP_VERSION.lstrip('V ')}")
        top_row.addWidget(self.lbl_version)
        
        top_row.addStretch()
        outer.addLayout(top_row)

        heading = QtWidgets.QLabel("Выберите основной инструмент для запуска")
        heading.setObjectName("homeHeading")
        heading.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(heading)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        small_index = 0
        for icon, title, description, tool_id, is_large in _TOOLS:
            card = _ToolCard(icon, title, description, is_large)
            card.clicked.connect(
                lambda _checked=False, tid=tool_id: self.tool_selected.emit(tid)
            )
            self.tool_buttons[tool_id] = card
            if is_large:
                grid.addWidget(card, 0, 0, 1, 2)
            else:
                row = 1 + small_index // 2
                col = small_index % 2
                small_index += 1
                grid.addWidget(card, row, col)
        outer.addLayout(grid)
        outer.addStretch(1)

        proxy_row = QtWidgets.QHBoxLayout()
        proxy_row.addWidget(self.proxy_status_label, 1)
        proxy_row.addWidget(self.proxy_button, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        outer.addLayout(proxy_row)

    @staticmethod
    def _settings_manager():
        app = QtWidgets.QApplication.instance()
        if app is None:
            return None
        get_manager = getattr(app, "get_settings_manager", None)
        if callable(get_manager):
            return get_manager()
        return getattr(app, "settings_manager", None)

    def _open_proxy_settings(self) -> None:
        settings_manager = self._settings_manager()
        if settings_manager is None:
            return

        from gemini_translator.ui.dialogs.proxy import ProxySettingsDialog
        from gemini_translator.ui.overlay_host import present_dialog

        dialog = ProxySettingsDialog(self, settings_manager)
        present_dialog(self, dialog, lambda _result: self._refresh_proxy_status())

    def _refresh_proxy_status(self) -> None:
        settings_manager = self._settings_manager()
        self.proxy_button.setEnabled(settings_manager is not None)
        settings = settings_manager.load_proxy_settings() if settings_manager is not None else {}
        self._update_proxy_display(settings)

    def _update_proxy_display(self, settings: dict) -> None:
        enabled = bool(settings.get("enabled", False))
        proxy_type = str(settings.get("type") or "SOCKS5")
        host = str(settings.get("host") or "")
        port = str(settings.get("port") or "")
        user = str(settings.get("user") or "")

        if enabled and host and port:
            self.proxy_status_label.setText(f"Прокси: {proxy_type}://{host}:{port}")
            tooltip_lines = [f"Тип: {proxy_type}", f"Хост: {host}", f"Порт: {port}"]
            if user:
                tooltip_lines.append(f"Пользователь: {user}")
            self.proxy_status_label.setToolTip("\n".join(tooltip_lines))
        else:
            self.proxy_status_label.setText("Прокси: выключен")
            self.proxy_status_label.setToolTip("Сетевые запросы идут без прокси.")

    # --- Обновления --------------------------------------------------------
    #
    # HomePage — только координатор: диалоги, прогресс, отмена, состояние.
    # Сеть и git — в updater.py (рабочие потоки), установка — в
    # update_installer.py (отсоединённые хелперы с журналом и health-токеном).

    @staticmethod
    def _updater_settings():
        return QtCore.QSettings("SiberianTeam", "TranslatorFork")

    def _set_update_state(self, state):
        self._update_state = state
        idle = state is upd.UpdateState.IDLE
        self.btn_check_update.setEnabled(idle)
        if idle:
            self.btn_check_update.setText("Проверить обновления")

    def check_for_updates(self, silent=False):
        if self._update_state is not upd.UpdateState.IDLE:
            return
        settings = self._updater_settings()
        # Миграция: единственная истина о бинарной установке — встроенная
        # идентичность сборки, легаси-ключи больше не читаются и не пишутся.
        settings.remove("updater/installed_version")
        settings.remove("updater/installed_commit")
        self._update_silent = silent
        if silent and upd.detect_update_channel() is upd.UpdateChannel.DEVELOPMENT:
            return
        self._set_update_state(upd.UpdateState.CHECKING)
        self.btn_check_update.setText("Проверка...")
        settings_manager = self._settings_manager()
        self._update_checker = upd.UpdateChecker(
            self, manual=not silent,
            session_factory=lambda: upd.build_updater_session(settings_manager))
        self._update_checker.update_available.connect(self._on_update_info)
        self._update_checker.no_update.connect(self._on_no_update)
        self._update_checker.error_occurred.connect(self._on_update_error)
        self._update_checker.start()

    def _on_no_update(self):
        self._set_update_state(upd.UpdateState.IDLE)
        if self._update_silent:
            return
        QtWidgets.QMessageBox.information(
            self, "Обновление", "У вас установлена последняя версия программы.")

    def _on_update_error(self, err):
        self._set_update_state(upd.UpdateState.IDLE)
        if self._update_silent:
            self._last_silent_error = err
            from gemini_translator.utils.update_installer import log_update_event
            log_update_event(f"silent check failed: {err}")
            if not self._silent_retry_scheduled:
                self._silent_retry_scheduled = True
                QtCore.QTimer.singleShot(
                    30 * 60 * 1000, lambda: self.check_for_updates(silent=True))
            return
        extra = ""
        if self._last_silent_error and self._last_silent_error != err:
            extra = f"\n\nПоследняя фоновая ошибка: {self._last_silent_error}"
        QtWidgets.QMessageBox.warning(
            self, "Ошибка", f"Не удалось проверить обновления: {err}{extra}")

    def _on_update_info(self, info):
        self._set_update_state(upd.UpdateState.IDLE)
        settings = self._updater_settings()
        if self._update_silent:
            if info.suppress_id == settings.value("updater/ignored_version", ""):
                return
            if info.manual and info.kind == "archive":
                # Неизвестная идентичность архива: не нагнетаем при каждом
                # запуске, ручная проверка покажет полное объяснение.
                return
        action = self._present_update_dialog(info)
        if action == "ignore":
            settings.setValue("updater/ignored_version", info.suppress_id)
            return
        if action != "install":
            return
        if info.manual:
            import webbrowser
            webbrowser.open(info.manual_url or upd.RELEASES_PAGE)
            return
        if info.kind == "release":
            self._start_release_download(info)
        elif info.kind == "git":
            self._start_git_update(info)
        elif info.kind == "archive":
            self._start_archive_download(info)

    def _present_update_dialog(self, info) -> str:
        """Показывает диалог обновления; возвращает install/later/ignore."""
        import re
        html_desc = info.description.replace("\n", "<br>")
        html_desc = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", html_desc)
        html_desc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', html_desc)
        install_label = "Открыть страницу загрузки" if info.manual else "Скачать и установить"
        dialog = _UpdateAvailableDialog(
            info.title_version, html_desc, install_label, self
        )
        from gemini_translator.ui.overlay_host import exec_dialog
        exec_dialog(self, dialog)
        return dialog.action

    # -- загрузка релизного ассета --

    def _make_download_progress(self, text):
        progress = QtWidgets.QProgressDialog(text, "Отмена", 0, 100, self)
        progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        return progress

    def _start_release_download(self, info):
        from gemini_translator.utils import update_installer as inst
        self._set_update_state(upd.UpdateState.DOWNLOADING)
        self.btn_check_update.setText("Загрузка...")
        shape = None
        name = info.asset.name.lower()
        if name.endswith(".exe"):
            shape = "pe"
        elif name.endswith(".zip"):
            shape = "zip"
        settings_manager = self._settings_manager()
        self._downloader = upd.UpdateDownloader(
            info.asset.url, inst.staging_root(),
            expected_size=info.asset.size, expected_sha256=info.asset.sha256,
            shape=shape,
            session_factory=lambda: upd.build_updater_session(settings_manager),
            parent=self)
        self._wire_downloader(info, self._downloader,
                              on_verified=self._prepare_release_install)
        self._downloader.start()

    def _start_archive_download(self, info):
        from gemini_translator.utils import update_installer as inst
        self._set_update_state(upd.UpdateState.DOWNLOADING)
        self.btn_check_update.setText("Загрузка...")
        settings_manager = self._settings_manager()
        self._downloader = upd.UpdateDownloader(
            info.zip_url, inst.staging_root(), shape="zip",
            session_factory=lambda: upd.build_updater_session(settings_manager),
            parent=self)
        self._wire_downloader(info, self._downloader,
                              on_verified=self._prepare_archive_install)
        self._downloader.start()

    def _wire_downloader(self, info, downloader, on_verified):
        progress = self._make_download_progress("Загрузка обновления...")
        self._download_progress = progress

        def on_progress(done, total):
            if total:
                progress.setMaximum(100)
                progress.setValue(min(99, int(100 * done / total)))
            else:
                progress.setRange(0, 0)

        downloader.progress.connect(on_progress)
        progress.canceled.connect(downloader.cancel)
        downloader.verified.connect(lambda path: (progress.close(),
                                                  on_verified(info, path)))
        downloader.cancelled.connect(lambda: (progress.close(),
                                              self._set_update_state(upd.UpdateState.IDLE)))

        def on_failed(err):
            progress.close()
            self._set_update_state(upd.UpdateState.IDLE)
            QtWidgets.QMessageBox.critical(
                self, "Ошибка загрузки", f"Не удалось скачать обновление: {err}")

        downloader.failed.connect(on_failed)
        progress.show()

    # -- подготовка установки --

    def _install_context(self, version_label):
        from gemini_translator.utils import update_installer as inst
        return inst.InstallContext(
            app_pid=os.getpid(),
            real_executable=inst.get_real_executable(),
            version_label=version_label)

    def _prepare_release_install(self, info, staged_path):
        from gemini_translator.utils import update_installer as inst
        self._set_update_state(upd.UpdateState.PREPARING)
        channel = upd.detect_update_channel()
        ctx = self._install_context(f"v{info.title_version}")

        def job():
            if channel is upd.UpdateChannel.WINDOWS_INSTALLED:
                inst.prepare_windows_installed(staged_path, ctx)
            elif channel is upd.UpdateChannel.WINDOWS_PORTABLE:
                inst.prepare_windows_portable(staged_path, ctx)
            elif channel is upd.UpdateChannel.MACOS:
                inst.prepare_macos(staged_path, ctx)
            else:
                raise inst.UpdateInstallError(
                    "Этот тип установки не поддерживает автообновление")
            return None

        self._run_prepare_worker(job)

    def _prepare_archive_install(self, info, staged_path):
        from gemini_translator.utils import update_installer as inst
        self._set_update_state(upd.UpdateState.PREPARING)
        ctx = self._install_context(info.commit[:12])

        def job():
            inst.prepare_source_archive(staged_path, upd.project_root(), ctx,
                                        commit_sha=info.commit)
            return None

        self._run_prepare_worker(job)

    def _run_prepare_worker(self, job):
        self._update_worker = upd.FunctionWorker(job, self)
        self._update_worker.done.connect(lambda _result: self._begin_exit())
        self._update_worker.failed.connect(self._on_prepare_failed)
        self._update_worker.start()

    def _on_prepare_failed(self, err):
        self._set_update_state(upd.UpdateState.IDLE)
        QtWidgets.QMessageBox.critical(
            self, "Ошибка обновления",
            f"Не удалось подготовить установку обновления:\n{err}\n\n"
            "Установка не начиналась, текущая версия не изменена — "
            "можно повторить попытку.")

    # -- git-обновление --

    def _start_git_update(self, info):
        self._set_update_state(upd.UpdateState.PREPARING)
        progress = QtWidgets.QProgressDialog(
            "Получение обновлений (git pull --ff-only --autostash)...",
            None, 0, 0, self)
        progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        progress.setCancelButton(None)
        progress.show()

        def job():
            from gemini_translator.utils import update_installer as inst
            return inst.install_git_update(upd.project_root())

        self._update_worker = upd.FunctionWorker(job, self)

        def on_done(_result):
            progress.close()
            from PyQt6.QtCore import QProcess
            if not QProcess.startDetached(sys.executable, sys.argv):
                self._set_update_state(upd.UpdateState.IDLE)
                QtWidgets.QMessageBox.critical(
                    self, "Ошибка перезапуска",
                    "Код обновлён, но перезапустить программу не удалось. "
                    "Закройте и откройте её вручную.")
                return
            self._begin_exit()

        def on_failed(err):
            progress.close()
            self._set_update_state(upd.UpdateState.IDLE)
            QtWidgets.QMessageBox.critical(self, "Ошибка обновления", err)

        self._update_worker.done.connect(on_done)
        self._update_worker.failed.connect(on_failed)
        self._update_worker.start()

    # -- штатное завершение --

    def _begin_exit(self):
        """Штатный выход: настройки, воркеры и туннели успевают завершиться.

        Хелпер ждёт завершения нашего PID; если пользователь отменил закрытие
        (ловушка closeEvent), хелпер увидит живой процесс и откажется от
        установки, ничего не тронув.
        """
        from gemini_translator.utils.update_installer import log_update_event
        self._set_update_state(upd.UpdateState.EXITING)
        self.btn_check_update.setEnabled(False)
        window = self.window()
        if window is not None:
            window.setProperty("is_updating", True)

        emergency = QtCore.QTimer(self)
        emergency.setSingleShot(True)

        def _emergency_exit():
            log_update_event("emergency exit after shutdown timeout")
            os._exit(0)

        emergency.timeout.connect(_emergency_exit)
        emergency.start(15000)

        log_update_event("orderly shutdown requested for update")
        if window is not None:
            window.close()
            if window.isVisible():
                # Пользователь отменил закрытие — обновление отменяется.
                emergency.stop()
                log_update_event("shutdown cancelled by user; update aborted")
                self._set_update_state(upd.UpdateState.IDLE)
                return
        app = QtWidgets.QApplication.instance()
        if app is not None:
            QtCore.QTimer.singleShot(0, app.quit)
