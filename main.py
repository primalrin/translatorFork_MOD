# -*- coding: utf-8 -*-
import sys
import os
import os_patch
import builtins
import argparse
import traceback
import asyncio
import sqlite3
import atexit
import base64
import importlib
import subprocess
from pathlib import Path
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication
from gemini_translator.api.managers import ApiKeyManager
from gemini_translator.utils.glossary_tools import ContextManager
from gemini_translator.ui.dialogs.setup import InitialSetupDialog
from gemini_translator.ui.dialogs.misc import StartupToolDialog
from gemini_translator.ui.dialogs.glossary import MainWindow as GlossaryToolWindow
from gemini_translator.ui.dialogs.validation import TranslationValidatorDialog
from gemini_translator.utils.settings import SettingsManager
from gemini_translator.utils.project_manager import TranslationProjectManager
from gemini_translator.core.translation_engine import TranslationEngine
from gemini_translator.api import config as api_config
from gemini_translator.core.task_manager import ChapterQueueManager
from gemini_translator.utils.proxy_tool import GlobalProxyController
from gemini_translator.utils.server_manager import ServerManager
from window_branding import install_window_title_branding
from gemini_translator.ui.dialogs.proxy import ProxySettingsDialog
from gemini_translator.ui.themes import (
    DARK_STYLESHEET,
    build_dark_stylesheet,
    extract_theme_colors,
)

# ---------------------------------------------------------------------------
# Gemini EPUB Translator - Точка входа в приложение
APP_VERSION = "V 10.5.17"  # <-- ОПРЕДЕЛЕНИЕ ВЕРСИИ ЗДЕСЬ

# ---------------------------------------------------------------------------
# Этот файл отвечает за запуск приложения, обработку аргументов командной
# строки и выбор режима работы (автоматический, параллельный, гибридный).
# Вся основная логика, классы окон и утилиты импортируются из пакета
# 'gemini_translator'.
# ---------------------------------------------------------------------------


# --- БЛОК: АВАРИЙНЫЙ ПРОСМОТРЩИК ОШИБОК ---


RANOBELIB_BUNDLED_DIRNAME = "ranobelib"
RANOBELIB_MODULE_NAMES = (
    "api_upload",
    "constants",
    "dialogs",
    "main_window",
    "models",
    "parsers",
    "utils",
    "workers",
)


def configure_ranobelib_playwright_runtime():
    if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        try:
            current_policy = asyncio.get_event_loop_policy()
        except Exception:
            current_policy = None
        if not isinstance(current_policy, asyncio.WindowsProactorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    resolved_paths = {
        "PLAYWRIGHT_BROWSERS_PATH": api_config.find_playwright_browsers_path(),
        "PLAYWRIGHT_NODEJS_PATH": api_config.find_node_executable(),
        "PLAYWRIGHT_PACKAGE_ROOT": api_config.find_playwright_package_root(),
    }
    for env_name, resolved_path in resolved_paths.items():
        if not resolved_path:
            continue
        path_obj = Path(resolved_path)
        if path_obj.exists():
            os.environ[env_name] = str(path_obj)


def patch_ranobelib_login_worker():
    workers_module = importlib.import_module("workers")
    login_worker_class = getattr(workers_module, "LoginWorker", None)
    if not login_worker_class or getattr(login_worker_class, "_translatorfork_patched", False):
        return

    def verify_ranobelib_session(profile_dir):
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                    viewport={"width": 1280, "height": 900},
                    args=workers_module.BROWSER_ARGS,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto("https://ranobelib.me", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)
                    has_auth = page.evaluate(
                        """() => {
                            try {
                                const raw = localStorage.getItem("auth");
                                if (!raw) {
                                    return false;
                                }
                                const parsed = JSON.parse(raw);
                                return !!(parsed && parsed.token && parsed.token.access_token);
                            } catch (error) {
                                return false;
                            }
                        }"""
                    )
                    return bool(has_auth), None
                finally:
                    context.close()
        except Exception as error:
            return False, str(error)

    def patched_run(self):
        debug_log_path = None

        def append_debug(message):
            if not debug_log_path:
                return
            try:
                debug_log_path.parent.mkdir(parents=True, exist_ok=True)
                with debug_log_path.open("a", encoding="utf-8") as debug_file:
                    debug_file.write(message.rstrip() + "\n")
            except Exception:
                pass

        try:
            profile_dir = (
                workers_module.BROWSER_PROFILE_DIR
                if self._site == "ranobelib"
                else workers_module.BROWSER_RULATE_DIR
            )
            debug_log_path = Path(profile_dir).parent / "login_worker_debug.log"
            start_url = (
                "https://ranobelib.me"
                if self._site == "ranobelib"
                else "https://tl.rulate.ru"
            )
            site_label = "RanobeLib" if self._site == "ranobelib" else "Rulate"
            append_debug(
                f"[START] site={self._site} profile={profile_dir} "
                f"browsers={os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')} "
                f"node={os.environ.get('PLAYWRIGHT_NODEJS_PATH', '')} "
                f"pkg={os.environ.get('PLAYWRIGHT_PACKAGE_ROOT', '')}"
            )

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                self._browser = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    args=workers_module.BROWSER_ARGS,
                )
                page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
                try:
                    page.goto(start_url, timeout=60000)
                except Exception:
                    pass
                self.log_signal.emit(
                    "WARNING",
                    f">>> ВОЙДИТЕ В АККАУНТ {site_label} И ЗАКРОЙТЕ БРАУЗЕР <<<",
                )
                append_debug(f"[BROWSER_OPENED] site={self._site}")
                try:
                    while True:
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
                append_debug(f"[BROWSER_CLOSED] site={self._site}")

            if self._site == "ranobelib":
                auth_detected, verify_error = verify_ranobelib_session(profile_dir)
                if auth_detected:
                    append_debug("[VERIFY_OK] ranobelib auth detected")
                    self.log_signal.emit(
                        "SUCCESS",
                        "Авторизация RanobeLib сохранена. Браузер можно не открывать повторно.",
                    )
                elif verify_error:
                    append_debug(f"[VERIFY_WARN] {verify_error}")
                    self.log_signal.emit(
                        "WARNING",
                        f"Браузер RanobeLib закрыт, но проверить сохранённую сессию не удалось: {verify_error}",
                    )
                else:
                    append_debug("[VERIFY_FAIL] ranobelib auth not found")
                    self.log_signal.emit(
                        "ERROR",
                        "Авторизация RanobeLib не обнаружена в сохранённом профиле. "
                        "Войдите в аккаунт и дождитесь полной загрузки страницы перед закрытием браузера.",
                    )
            else:
                append_debug("[SUCCESS] rulate cookies saved")
                self.log_signal.emit("SUCCESS", f"Браузер {site_label} закрыт. Куки сохранены.")
        except Exception as error:
            append_debug(f"[ERROR] {type(error).__name__}: {error}")
            append_debug(traceback.format_exc())
            self.log_signal.emit("ERROR", f"Ошибка авторизации: {error}")
        finally:
            append_debug("[FINISH]")
            self.finished_signal.emit()

    def stable_patched_run(self):
        debug_log_path = None

        def append_debug(message):
            if not debug_log_path:
                return
            try:
                debug_log_path.parent.mkdir(parents=True, exist_ok=True)
                with debug_log_path.open("a", encoding="utf-8") as debug_file:
                    debug_file.write(message.rstrip() + "\n")
            except Exception:
                pass

        try:
            profile_dir = (
                workers_module.BROWSER_PROFILE_DIR
                if self._site == "ranobelib"
                else workers_module.BROWSER_RULATE_DIR
            )
            debug_log_path = Path(profile_dir).parent / "login_worker_debug.log"
            start_url = (
                "https://ranobelib.me"
                if self._site == "ranobelib"
                else "https://tl.rulate.ru"
            )
            site_label = "RanobeLib" if self._site == "ranobelib" else "Rulate"
            append_debug(
                f"[START] site={self._site} profile={profile_dir} "
                f"browsers={os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')} "
                f"node={os.environ.get('PLAYWRIGHT_NODEJS_PATH', '')} "
                f"pkg={os.environ.get('PLAYWRIGHT_PACKAGE_ROOT', '')}"
            )

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                self._browser = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    args=workers_module.BROWSER_ARGS,
                )
                page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
                try:
                    page.goto(start_url, timeout=60000)
                except Exception:
                    pass
                self.log_signal.emit(
                    "WARNING",
                    f">>> ВОЙДИТЕ В АККАУНТ {site_label} И ЗАКРОЙТЕ БРАУЗЕР <<<",
                )
                append_debug(f"[BROWSER_OPENED] site={self._site}")
                try:
                    while True:
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
                append_debug(f"[BROWSER_CLOSED] site={self._site}")

            if self._site == "ranobelib":
                auth_detected, verify_error = verify_ranobelib_session(profile_dir)
                if auth_detected:
                    append_debug("[VERIFY_OK] ranobelib auth detected")
                    self.log_signal.emit(
                        "SUCCESS",
                        "Авторизация RanobeLib сохранена. Браузер можно не открывать повторно.",
                    )
                elif verify_error:
                    append_debug(f"[VERIFY_WARN] {verify_error}")
                    self.log_signal.emit(
                        "WARNING",
                        f"Браузер RanobeLib закрыт, но проверить сохранённую сессию не удалось: {verify_error}",
                    )
                else:
                    append_debug("[VERIFY_FAIL] ranobelib auth not found")
                    self.log_signal.emit(
                        "WARNING",
                        "Браузер RanobeLib закрыт, но автопроверка не нашла сохранённый токен. "
                        "Если вход был выполнен, продолжайте работу в режиме «Через браузер». "
                        "Для API-режима может понадобиться повторный вход.",
                    )
            else:
                append_debug("[SUCCESS] rulate cookies saved")
                self.log_signal.emit("SUCCESS", f"Браузер {site_label} закрыт. Куки сохранены.")
        except Exception as error:
            append_debug(f"[ERROR] {type(error).__name__}: {error}")
            append_debug(traceback.format_exc())
            self.log_signal.emit("ERROR", f"Ошибка авторизации: {error}")
        finally:
            append_debug("[FINISH]")
            self.finished_signal.emit()

    login_worker_class.run = stable_patched_run
    login_worker_class._translatorfork_patched = True


def iter_ranobelib_source_locations():
    seen = set()
    candidate_dirs = []

    bundled_base = getattr(sys, "_MEIPASS", None)
    if bundled_base:
        candidate_dirs.append(Path(bundled_base) / RANOBELIB_BUNDLED_DIRNAME)

    if getattr(sys, "frozen", False):
        candidate_dirs.append(Path(sys.executable).resolve().parent / RANOBELIB_BUNDLED_DIRNAME)

    candidate_dirs.append(Path(__file__).resolve().parent / RANOBELIB_BUNDLED_DIRNAME)

    for candidate in candidate_dirs:
        normalized = str(candidate.resolve(strict=False)).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        yield candidate


def resolve_ranobelib_source_dir():
    searched_locations = []

    for base_dir in iter_ranobelib_source_locations():
        searched_locations.append(str(base_dir))
        if (base_dir / "main_window.py").exists():
            return base_dir, searched_locations

    return None, searched_locations


def build_ranobelib_window():
    source_dir, searched_locations = resolve_ranobelib_source_dir()
    if not source_dir:
        checked_paths = "\n".join(f"- {path}" for path in searched_locations) or "- (нет проверенных путей)"
        raise FileNotFoundError(
            "Не удалось найти встроенные исходники RanobeLib.\n\n"
            f"Проверенные пути:\n{checked_paths}"
        )

    source_dir_str = str(source_dir)
    sys.path = [path for path in sys.path if path != source_dir_str]
    sys.path.insert(0, source_dir_str)
    configure_ranobelib_playwright_runtime()

    for module_name in RANOBELIB_MODULE_NAMES:
        sys.modules.pop(module_name, None)

    importlib.invalidate_caches()
    import docx  # noqa: F401
    patch_ranobelib_login_worker()

    main_window_module = importlib.import_module("main_window")
    window_class = getattr(main_window_module, "RanobeUploaderApp")
    window = window_class()

    if hasattr(window, "set_return_to_menu_handler"):
        def return_to_menu():
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.exit(EXIT_CODE_REBOOT)

        window.set_return_to_menu_handler(return_to_menu)

    return window


def launch_ranobelib_uploader():
    try:
        return build_ranobelib_window(), False
    except Exception as direct_error:
        source_dir, searched_locations = resolve_ranobelib_source_dir()
        checked_paths = "\n".join(f"- {path}" for path in searched_locations) or "- (нет проверенных путей)"
        source_status = f"Источник RanobeLib: {source_dir}" if source_dir else "Источник RanobeLib не найден."

        QtWidgets.QMessageBox.critical(
            None,
            "Ошибка запуска RanobeLib",
            f"Не удалось открыть встроенный RanobeLib.\n\n"
            f"{type(direct_error).__name__}: {direct_error}\n\n"
            f"{source_status}\n\n"
            f"Проверенные пути исходников:\n{checked_paths}"
        )
        return None, False


def build_gemini_reader_window():
    import gemini_reader_v3

    window = gemini_reader_v3.MainWindow()
    if hasattr(window, "set_return_to_menu_handler"):
        def return_to_menu():
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.exit(EXIT_CODE_REBOOT)

        window.set_return_to_menu_handler(return_to_menu)

    return window


def launch_gemini_reader():
    try:
        return build_gemini_reader_window(), False
    except Exception as direct_error:
        QtWidgets.QMessageBox.critical(
            None,
            "Ошибка запуска Gemini Reader",
            f"Не удалось открыть Gemini Reader.\n\n{type(direct_error).__name__}: {direct_error}",
        )
        return None, False


def prepare_console_streams():
    """
    Делает stdout/stderr устойчивыми к символам, которых нет в текущей
    кодировке консоли.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if not stream or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def apply_saved_app_theme(app, settings_manager=None):
    manager = settings_manager or getattr(app, "settings_manager", None)
    if manager is None:
        app.setStyleSheet(DARK_STYLESHEET)
        return

    theme_colors = {}
    for loader_name in ("load_full_session_settings", "load_settings"):
        loader = getattr(manager, loader_name, None)
        if not callable(loader):
            continue
        try:
            theme_colors = extract_theme_colors(loader())
        except Exception:
            theme_colors = {}
        if theme_colors:
            break

    app.setStyleSheet(build_dark_stylesheet(theme_colors))


def run_emergency_viewer():
    """
    Запускает минималистичный, самодостаточный диалог для отображения
    критической ошибки, когда основное приложение не отвечает.
    """
    # Минимальная темная тема, чтобы окно не выглядело чужеродно
    FALLBACK_DARK_QSS = """
        QDialog, QWidget { background-color: #2c313c; color: #f0f0f0; }
        QTextEdit { background-color: #1e222a; border: 1px solid #4d5666; }
        QPushButton { background-color: #4d5666; border: none; padding: 8px; border-radius: 4px; }
        QPushButton:hover { background-color: #5a6475; }
    """

    app = QtWidgets.QApplication(sys.argv)
    install_window_title_branding(app)
    app.setStyleSheet(FALLBACK_DARK_QSS)

    error_text = "Тестовое сообщение об ошибке.\n\nАргументы командной строки не были предоставлены."
    # Ошибка передается как второй аргумент (первый -- --emergency-viewer)
    if len(sys.argv) > 2:
        try:
            encoded_message = sys.argv[2]
            decoded_bytes = base64.b64decode(encoded_message)
            error_text = decoded_bytes.decode('utf-8')
        except Exception as e:
            error_text = f"Не удалось декодировать сообщение об ошибке: {e}\n\nИсходные данные:\n{sys.argv[2]}"

    # Создаем диалог напрямую, без доп. классов
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle("Аварийный Отчет об Ошибке")
    dialog.setMinimumSize(700, 500)

    layout = QtWidgets.QVBoxLayout(dialog)

    info_label = QtWidgets.QLabel(
        "Произошла критическая ошибка, которая привела к зависанию основного приложения.\n"
        "Это аварийное окно было запущено для отображения информации о сбое."
    )
    info_label.setWordWrap(True)
    info_label.setStyleSheet(
        "padding: 5px; background-color: #c0392b; color: white; border-radius: 4px;")
    layout.addWidget(info_label)

    details_view = QtWidgets.QTextEdit()
    details_view.setReadOnly(True)
    details_view.setFont(QtGui.QFont("Consolas", 10))
    details_view.setText(error_text)
    layout.addWidget(details_view)

    button_layout = QtWidgets.QHBoxLayout()
    copy_button = QtWidgets.QPushButton("Скопировать ошибку")

    def copy_action():
        QtWidgets.QApplication.clipboard().setText(error_text)
        copy_button.setText("Скопировано!")
        copy_button.setEnabled(False)
        reset_timer = getattr(dialog, "_copy_reset_timer", None)
        if reset_timer is None:
            reset_timer = QtCore.QTimer(dialog)
            reset_timer.setSingleShot(True)

            def reset_copy_button():
                copy_button.setText("Скопировать ошибку")
                copy_button.setEnabled(True)

            reset_timer.timeout.connect(reset_copy_button)
            dialog._copy_reset_timer = reset_timer

        reset_timer.start(2000)
        return
        QtCore.QTimer.singleShot(2000, lambda: (
            copy_button.setText("Скопировать ошибку"),
            copy_button.setEnabled(True)
        ))

    copy_button.clicked.connect(copy_action)

    close_button = QtWidgets.QPushButton("Закрыть")
    close_button.clicked.connect(dialog.accept)

    button_layout.addWidget(copy_button)
    button_layout.addStretch()
    button_layout.addWidget(close_button)
    layout.addLayout(button_layout)

    dialog.exec()
    sys.exit(0)  # Завершаем аварийный процесс


class LoadingDialog(QtWidgets.QDialog):
    """Простой диалог-заставка, который показывается во время инициализации."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Убираем рамку окна, делаем его похожим на заставку
        self.setWindowFlags(Qt.WindowType.SplashScreen |
                            Qt.WindowType.FramelessWindowHint)
        self.setModal(True)  # Блокируем другие окна, пока это видимо

        layout = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel("Инициализация приложения…")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("font-size: 12pt; padding: 20px;")
        layout.addWidget(self.label)
        self.setFixedSize(300, 100)


class ValidatorStartupDialog(QtWidgets.QDialog):
    """Новый диалог для выбора способа запуска Валидатора."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Запуск инструмента проверки")
        self.setMinimumWidth(400)
        self.output_folder = None
        self.original_epub_path = None
        self.settings_manager = SettingsManager()

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Выберите, как запустить инструмент проверки:"))

        history_btn = QtWidgets.QPushButton("📂 Загрузить проект из истории")
        history_btn.clicked.connect(self.load_from_history)
        layout.addWidget(history_btn)

        manual_btn = QtWidgets.QPushButton("✍️ Выбрать папку и файл вручную")
        manual_btn.clicked.connect(self.select_manually)
        layout.addWidget(manual_btn)

        cancel_btn = QtWidgets.QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn)

    def load_from_history(self):
        history = self.settings_manager.load_project_history()
        if not history:
            QtWidgets.QMessageBox.information(
                self, "История пуста", "Вы еще не запускали ни одного перевода.")
            return
        from gemini_translator.ui.dialogs.setup import ProjectHistoryDialog
        dialog = ProjectHistoryDialog(history, self.settings_manager, self)
        if dialog.exec():
            project = dialog.get_selected_project()
            if project:
                self.output_folder = project.get("output_folder")
                if self.output_folder:
                    self.settings_manager.save_last_project_folder(self.output_folder)
                self.original_epub_path = project.get("epub_path")
                if self.output_folder and (
                    not self.original_epub_path or not os.path.exists(self.original_epub_path)
                ):
                    self.original_epub_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                        self,
                        "Выберите исходный EPUB для проекта",
                        self.output_folder,
                        "*.epub"
                    )
                    if not self.original_epub_path:
                        return
                    self.settings_manager.add_to_project_history(self.original_epub_path, self.output_folder)
                if (
                    self.output_folder
                    and self.original_epub_path
                    and os.path.isdir(self.output_folder)
                    and os.path.exists(self.original_epub_path)
                ):
                    self._finalize_project_selection()
                else:
                    QtWidgets.QMessageBox.warning(
                        self, "Ошибка", "Пути в выбранном проекте недействительны.")

    def select_manually(self):
        start_folder = self.settings_manager.get_last_project_folder()
        if not start_folder or not os.path.isdir(start_folder):
            start_folder = os.path.expanduser("~")
        folder_dialog = QtWidgets.QFileDialog(self)
        folder_dialog.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
        folder_dialog.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
        folder_dialog.setDirectory(start_folder)
        if folder_dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        folder = folder_dialog.selectedFiles()[0]
        self.settings_manager.save_last_project_folder(folder)

        epub, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите исходный EPUB", folder, "*.epub")
        if not epub:
            return

        self.output_folder = folder
        self.original_epub_path = epub
        self._finalize_project_selection()
        return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Выберите папку с переводами")
        if not folder:
            return

        epub, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите исходный EPUB", "", "*.epub")
        if not epub:
            return

        self.output_folder = folder
        self.original_epub_path = epub
        self._finalize_project_selection()

    def _finalize_project_selection(self):
        self._offer_cleanup_old_translations()
        self.accept()

    def _offer_cleanup_old_translations(self):
        if not self.output_folder or not self.original_epub_path:
            return

        try:
            project_manager = TranslationProjectManager(self.output_folder)
            stale_entries = project_manager.find_stale_translations(self.original_epub_path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Проверка проекта",
                f"Не удалось проверить проект на старые переводы:\n{exc}"
            )
            return

        if not stale_entries:
            return

        preview = "\n".join(
            f"- {os.path.basename(entry['full_path'])}"
            for entry in stale_entries[:6]
        )
        if len(stale_entries) > 6:
            preview += f"\n- ... и еще {len(stale_entries) - 6}"

        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Очистка старых переводов")
        msg_box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        msg_box.setText(
            f"Найдено {len(stale_entries)} старых файлов перевода, которые относятся к главам, "
            "уже отсутствующим в выбранном EPUB."
        )
        msg_box.setInformativeText(
            f"Их можно удалить сразу перед открытием проекта.\n\nПримеры:\n{preview}"
        )
        yes_button = msg_box.addButton("Да, очистить", QtWidgets.QMessageBox.ButtonRole.YesRole)
        msg_box.addButton("Нет, оставить", QtWidgets.QMessageBox.ButtonRole.NoRole)
        msg_box.exec()

        if msg_box.clickedButton() != yes_button:
            return

        result = project_manager.cleanup_stale_translations(stale_entries)
        if result["failed"]:
            failed_preview = "\n".join(
                f"- {os.path.basename(path)}: {error}"
                for path, error in result["failed"][:6]
            )
            QtWidgets.QMessageBox.warning(
                self,
                "Очистка завершена частично",
                f"Удалено файлов: {result['removed']}.\n"
                f"Не удалось удалить: {len(result['failed'])}.\n\n{failed_preview}"
            )
            return

        QtWidgets.QMessageBox.information(
            self,
            "Очистка завершена",
            f"Удалено старых файлов перевода: {result['removed']}."
        )


def restart_with_new_files(epub_path, chapters):
    """Готовит приложение к перезапуску с новым набором файлов."""
    print("Подготовка к перезапуску с новыми файлами…")
    RESTART_INFO["is_restarting"] = True
    RESTART_INFO["epub_path"] = epub_path
    RESTART_INFO["chapters"] = chapters

    app = QtWidgets.QApplication.instance()
    if app:
        # Просто выходим из текущего цикла событий, чтобы вернуться в main()
        app.quit()


def global_excepthook(exc_type, exc_value, exc_tb):
    """
    Обрабатывает все неперехваченные исключения.
    Если приложение отвечает, показывает встроенное окно.
    Если приложение зависло, пытается грациозно завершить фоновые потоки
    и только потом запускает аварийный режим.
    """
    tb_list = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_str = "".join(tb_list)
    error_message = (
        f"Произошла неперехваченная ошибка: {exc_type.__name__}\n\n"
        f"--- Полный Traceback ---\n{tb_str}"
    )
    print(f"КРИТИЧЕСКАЯ ОШИБКА (Unhandled Exception):\n{error_message}")

    app = QtWidgets.QApplication.instance()

    # Сценарий 1: Приложение "живо" и может показать окно само.
    if app:
        try:
            # Даем приложению 100мс на обработку события перед показом окна
            # Это может помочь, если ошибка произошла в момент отрисовки
            QtCore.QTimer.singleShot(100, lambda: (
                QtWidgets.QMessageBox.critical(
                    None, "Критическая Ошибка Приложения", error_message
                ),
                # Запрашиваем штатное завершение, которое вызовет все aboutToQuit сигналы
                QtCore.QTimer.singleShot(0, app.quit)
            ))
            return
        except Exception as e:
            print(
                f"[CRITICAL] Не удалось показать QMessageBox, даже при живом app: {e}")
            # Если даже QMessageBox падает, переходим к плану "Б".

    # --- НОВЫЙ БЛОК: Попытка грациозного завершения ---
    # Это выполняется, только если приложение не отвечает.
    print("[CRITICAL] Приложение Qt не отвечает. Попытка принудительной, но грациозной остановки...")
    if app and hasattr(app, 'engine') and hasattr(app, 'engine_thread'):
        try:
            # Отправляем команду на очистку в поток движка и ждем его завершения
            # с таймаутом, чтобы не зависнуть здесь навсегда.
            print("[CRITICAL] Отправка команды cleanup в движок...")
            app.engine.cancel_translation("Аварийное завершение по ошибке")

            # Ждем завершения потока движка (он должен сам себя остановить)
            if app.engine_thread.wait(5000):  # Ждем до 5 секунд
                print("[CRITICAL] Фоновые потоки успешно завершены.")
            else:
                print(
                    "[CRITICAL] Таймаут ожидания фоновых потоков. Возможны 'зомби'.")
        except Exception as e:
            print(
                f"[CRITICAL] Ошибка во время попытки грациозного завершения: {e}")
    # --- КОНЕЦ НОВОГО БЛОКА ---

    # Сценарий 2: Запускаем "Спасательную шлюпку".
    print("[CRITICAL] Запуск аварийного просмотрщика ошибок...")
    try:
        import subprocess

        encoded_message = base64.b64encode(
            error_message.encode('utf-8')).decode('ascii')

        # Запускаем самих себя со специальным флагом.
        command = [sys.executable, sys.argv[0],
                   '--emergency-viewer', encoded_message]

        # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ---
        # Запускаем дочерний процесс в "чистом" системном окружении,
        # чтобы он не наследовал пути к временной папке умирающего родителя.
        subprocess.Popen(command, env=os.environ.copy())
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    except Exception as e:
        print(
            f"[ULTRA-CRITICAL] Не удалось запустить аварийный просмотрщик: {e}")

    # Принудительно завершаем зависший процесс, как и раньше.
    os._exit(1)


class ApplicationWithContext(QtWidgets.QApplication):
    """
    Расширенный класс QApplication для управления активным контекстом настроек.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._true_global_settings_manager = None
        self._active_settings_manager = None

    def initialize_managers(self):
        """Инициализирует менеджеры после создания основного объекта."""
        # Этот менеджер - константа. Он всегда работает с файлом ~/.epub_translator/settings.json
        self._true_global_settings_manager = SettingsManager(
            event_bus=self.event_bus)
        # По умолчанию активный менеджер - это глобальный
        self._active_settings_manager = self._true_global_settings_manager

    def get_settings_manager(self) -> SettingsManager:
        """
        ЕДИНСТВЕННЫЙ правильный способ получить текущий менеджер настроек.
        Все виджеты должны использовать этот метод.
        """
        return self._active_settings_manager

    def get_server_manager(self):
        """Возвращает менеджер сервера Perplexity."""
        return self.server_manager


class EventBus(QtCore.QObject):
    import threading
    class _TopicEmitter(QtCore.QObject):
        event_posted = QtCore.pyqtSignal(dict)

    event_posted = QtCore.pyqtSignal(dict)
    # Новые атрибуты и методы для "шины с инерцией"
    # Сигнал, который передает ключ измененных данных
    data_changed = QtCore.pyqtSignal(str)
    _data_store = {}
    _lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._topic_subscribers = {}
        self._topic_lock = self.threading.Lock()
        self.event_posted.connect(self._dispatch_to_topics)

    def subscribe(self, event_name: str, callback):
        """Подписывает callback только на конкретный тип события."""
        with self._topic_lock:
            subscribers = self._topic_subscribers.setdefault(event_name, [])
            if any(item[0] == callback for item in subscribers):
                return
            emitter = self._TopicEmitter(self)
            emitter.event_posted.connect(callback)
            subscribers.append((callback, emitter))

    def unsubscribe(self, event_name: str, callback):
        """Убирает callback из конкретной topic-подписки."""
        with self._topic_lock:
            subscribers = self._topic_subscribers.get(event_name)
            if not subscribers:
                return
            remaining = []
            for subscribed_callback, emitter in subscribers:
                if subscribed_callback == callback:
                    try:
                        emitter.event_posted.disconnect(callback)
                    except (TypeError, RuntimeError):
                        pass
                    emitter.deleteLater()
                else:
                    remaining.append((subscribed_callback, emitter))
            subscribers[:] = remaining
            if not subscribers:
                self._topic_subscribers.pop(event_name, None)

    def unsubscribe_all(self, callback):
        """Убирает callback из всех topic-подписок."""
        with self._topic_lock:
            empty_topics = []
            for event_name, subscribers in self._topic_subscribers.items():
                remaining = []
                for subscribed_callback, emitter in subscribers:
                    if subscribed_callback == callback:
                        try:
                            emitter.event_posted.disconnect(callback)
                        except (TypeError, RuntimeError):
                            pass
                        emitter.deleteLater()
                    else:
                        remaining.append((subscribed_callback, emitter))
                subscribers[:] = remaining
                if not subscribers:
                    empty_topics.append(event_name)
            for event_name in empty_topics:
                self._topic_subscribers.pop(event_name, None)

    def _dispatch_to_topics(self, event: dict):
        event_name = event.get('event') if isinstance(event, dict) else None
        if not event_name:
            return
        with self._topic_lock:
            emitters = [emitter for _callback, emitter in self._topic_subscribers.get(event_name, [])]
        for emitter in emitters:
            emitter.event_posted.emit(event)

    def emit_event(self, event: dict):
        """Совместимый способ отправки: topics + старый event_posted сигнал."""
        self.event_posted.emit(event)

    def set_data(self, key: str, value):
        """Потокобезопасно сохраняет данные и испускает сигнал."""
        with self._lock:
            self._data_store[key] = value
        self.data_changed.emit(key)

    def pop_data(self, key: str, default=None):
        """Потокобезопасно извлекает (и удаляет) данные."""
        with self._lock:
            return self._data_store.pop(key, default)

    def get_data(self, key: str, default=None):
        """Потокобезопасно читает данные, не удаляя их."""
        with self._lock:
            return self._data_store.get(key, default)


def initialize_global_resources(app: QApplication):
    """
    Создает ПУСТЫЕ глобальные ресурсы (БД, диск) и "вешает" их на QApplication.
    Не создает никаких таблиц!
    """
    print("--- Инициализация глобальных ресурсов... ---")
    try:
        # 1. Создаем и удерживаем "якорное" подключение к пустой БД
        # WAL (Write-Ahead Logging) позволяет нескольким потокам читать,
        # пока один поток пишет, что предотвращает deadlock'и.
        main_db_conn = sqlite3.connect(
            api_config.SHARED_DB_URI, uri=True, check_same_thread=False)
        main_db_conn.row_factory = sqlite3.Row

        # --- Включаем WAL на главном соединении ---
        main_db_conn.execute("PRAGMA journal_mode=WAL;")
        # Ждать до 5 секунд
        main_db_conn.execute("PRAGMA busy_timeout = 5000;")

        atexit.register(lambda: main_db_conn.close())
        app.main_db_connection = main_db_conn
        print("--- Общая in-memory база данных активна и удерживается. ---")

    except Exception as e:
        raise RuntimeError(
            f"КРИТИЧЕСКАЯ ОШИБКА при инициализации глобальных ресурсов: {e}")


# ============================================================================
# ОСНОВНАЯ ТОЧКА ВХОДА
# ============================================================================
if len(sys.argv) > 1 and sys.argv[1] == '--emergency-viewer':
    run_emergency_viewer()

# Специальный код возврата для перезагрузки приложения (возврат в меню)
EXIT_CODE_REBOOT = 2000

if __name__ == "__main__":
    import threading
    prepare_console_streams()
    sys.excepthook = global_excepthook
    # --- РЕГИСТРАЦИЯ ГЛАВНОГО ПОТОКА ---
    main_id = threading.get_ident()
    print(f"\n[SYSTEM] MAIN UI THREAD ID: {main_id}\n")
    # Регистрируем его как VIP
    os_patch.PatientLock.register_vip_thread(main_id)

    app = ApplicationWithContext(sys.argv)
    install_window_title_branding(app)
    app.setStyleSheet(DARK_STYLESHEET)

    # Инициализация ресурсов (один раз при старте процесса)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    initialize_global_resources(app)

    # [ARCH] Активация виртуальной файловой системы.
    # С этого момента функции open(), os.path.* и zipfile умеют работать с путями 'mem://'.
    os_patch.apply()
    api_config.initialize_configs()

    print("[INFO] Инициализация основных сервисов приложения…")

    app.event_bus = EventBus()
    app.initialize_managers()
    app.settings_manager = app.get_settings_manager()
    apply_saved_app_theme(app, app.settings_manager)
    app.task_manager = ChapterQueueManager(event_bus=app.event_bus)
    app.global_version = APP_VERSION
    app.proxy_controller = GlobalProxyController(app.event_bus)
    proxy_settings = app.settings_manager.load_proxy_settings()

    temp_folder = os.path.join(
        os.path.expanduser("~"), ".epub_translator_temp")
    os.makedirs(temp_folder, exist_ok=True)
    app.context_manager = ContextManager(temp_folder)
    app.server_manager = ServerManager(app.event_bus)
    print("[INFO] Инициализация TranslationEngine…")

    app.engine = TranslationEngine(task_manager=app.task_manager)
    app.engine_thread = QtCore.QThread(app)
    app.engine.moveToThread(app.engine_thread)

    # Убираем автоматическую остановку потока по aboutToQuit,
    # чтобы движок переживал перезагрузку интерфейса (код 2000).
    # Ручная остановка выполняется в самом конце файла.

    app.engine_thread.finished.connect(app.engine.deleteLater)

    app.engine_thread.start()

    print("[OK] TranslationEngine запущен в фоновом потоке.")
    QtCore.QMetaObject.invokeMethod(
        app.engine,
        "log_thread_identity",
        QtCore.Qt.ConnectionType.QueuedConnection
    )

    try:
        import jieba
        print("[INFO] Warming up jieba dictionary…")
        jieba.lcut("прогрев", cut_all=False)
    except (ImportError, Exception) as e:
        print(f"[WARN] Could not warm up jieba dictionary: {e}")

    # --- ГЛАВНЫЙ ЦИКЛ ПРИЛОЖЕНИЯ ---
    while True:
        main_window_to_run = None
        loading_dialog = LoadingDialog()

        try:
            # Диалог выбора инструмента
            tool_dialog = StartupToolDialog(app_version=APP_VERSION)
            if tool_dialog.exec():
                selected_tool = tool_dialog.selected_tool
                if selected_tool == 'translator':
                    main_window_to_run = InitialSetupDialog()
                elif selected_tool == 'validator':
                    startup_dialog = ValidatorStartupDialog()
                    if startup_dialog.exec():
                        output_folder = startup_dialog.output_folder
                        original_epub = startup_dialog.original_epub_path
                        project_manager = TranslationProjectManager(
                            output_folder)
                        # retry_enabled=False означает автономный режим
                        main_window_to_run = TranslationValidatorDialog(
                            output_folder,
                            original_epub,
                            retry_enabled=False,
                            project_manager=project_manager
                        )
                elif selected_tool == 'glossary':
                    # Импортируем диалог запуска (он теперь внутри модуля)
                    from gemini_translator.ui.dialogs.glossary import GlossaryStartupDialog

                    startup_dialog = GlossaryStartupDialog()
                    if startup_dialog.exec():
                        # project_path может быть путем или None (если выбран пустой режим)
                        project_path = startup_dialog.project_path
                        main_window_to_run = GlossaryToolWindow(
                            mode='standalone',
                            project_path=project_path
                        )
                elif selected_tool == 'rulate_export':
                    from gemini_translator.ui.dialogs.rulate_export import (
                        RulateMarkdownExportWindow,
                    )

                    main_window_to_run = RulateMarkdownExportWindow()
                elif selected_tool == 'chapter_splitter':
                    from gemini_translator.ui.dialogs.chapter_splitter import (
                        ChapterSplitterWindow,
                    )

                    main_window_to_run = ChapterSplitterWindow()
                elif selected_tool == 'gemini_reader':
                    gemini_reader_window, _ = launch_gemini_reader()
                    if gemini_reader_window:
                        main_window_to_run = gemini_reader_window
                    else:
                        continue
                elif selected_tool == 'ranobelib_uploader':
                    ranobelib_window, _ = launch_ranobelib_uploader()
                    if ranobelib_window:
                        main_window_to_run = ranobelib_window
                    else:
                        continue
                elif selected_tool == 'prompt_benchmark':
                    from gemini_translator.ui.dialogs.benchmark import PromptBenchmarkDialog
                    main_window_to_run = PromptBenchmarkDialog()
            else:
                # Пользователь закрыл меню — выход
                break

        except Exception as e:
            if loading_dialog.isVisible():
                loading_dialog.close()

            tb_str = "".join(traceback.format_exception(
                type(e), e, e.__traceback__))
            error_message = (
                f"Произошла критическая ошибка при инициализации окна: {type(e).__name__}\n\n"
                f"--- Полный Traceback ---\n{tb_str}"
            )
            print(f"[CRITICAL STARTUP ERROR]\n{error_message}")
            QtWidgets.QMessageBox.critical(
                None, "Ошибка запуска", error_message)
            main_window_to_run = None

        # Запуск выбранного окна
        if main_window_to_run:
            main_window_to_run.show()
            exit_code = app.exec()

            # Если код возврата равен коду перезагрузки, цикл while повторится
            if exit_code != EXIT_CODE_REBOOT:
                break
        else:
            break

    # --- ЗАВЕРШЕНИЕ ---
    print(f"[INFO] Приложение завершает работу.")
    if hasattr(app, 'engine_thread') and app.engine_thread.isRunning():
        app.engine_thread.quit()
        app.engine_thread.wait()
    sys.exit(0)
