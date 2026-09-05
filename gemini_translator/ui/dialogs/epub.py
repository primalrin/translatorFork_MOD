# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# Диалоги для работы с EPUB
# ---------------------------------------------------------------------------
# Этот файл содержит классы диалоговых окон для взаимодействия с EPUB-файлами:
# - EpubHtmlSelectorDialog: выбор HTML-глав из EPUB.
# - TranslatedChaptersManagerDialog: управление переведенными главами и сборка EPUB.
# ---------------------------------------------------------------------------

import os
import sys
import re
import glob
import zipfile
import shutil
import tempfile
import json
import html as html_lib
import unicodedata
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
import traceback
from functools import partial
from collections import Counter
import io
import mimetypes
# --- Импорты из PyQt6 ---
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import pyqtSignal, pyqtSlot, QThread
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QListWidget, QPushButton, QListWidgetItem, QTableWidget,
    QTableWidgetItem, QGroupBox, QFormLayout, QFileDialog, QTextEdit, QMessageBox,
    QLabel, QDialogButtonBox, QHeaderView, QAbstractItemView, QWidget, QHBoxLayout,
    QComboBox, QApplication, QCheckBox, QTabWidget
)
# --- Импорты из сторонних библиотек ---
try:
    from bs4 import BeautifulSoup, NavigableString, Tag
    BS4_AVAILABLE = True
except ImportError:
    BeautifulSoup = None
    NavigableString = None
    Tag = None
    BS4_AVAILABLE = False

try:
    from recognizers_text import Culture
    from recognizers_number import recognize_number
    RECOGNIZERS_AVAILABLE = True
except ImportError:
    RECOGNIZERS_AVAILABLE = False

try:
    import Levenshtein
    LEVENSHTEIN_AVAILABLE = True
except ImportError:
    LEVENSHTEIN_AVAILABLE = False
    
# --- Импорты из нашего проекта ---
from gemini_translator.ui import theme_manager
from gemini_translator.ui.wait_dialogs import show_when_slow
from ...utils.epub_tools import get_epub_chapter_order, extract_number_from_path, extract_number_from_path_reversed, EpubCreator, TASK_SIZE_UNIT_CHARS, get_epub_chapter_sizes_with_cache, extract_epub_heading_text
from ...utils.text import unify_paragraphs_for_ai
from ...utils.project_manager import TranslationProjectManager
from ...utils.project_migrator import ProjectMigrator, SyncThread
from ...utils.translation_versions import sort_translation_versions_for_epub_build
from ..widgets.common_widgets import NoScrollComboBox
from .chapter_editor import ChapterEditorDialog
from ...core.epub_deep_cleanup_helpers import (
    DEEP_CLEANUP_RECOMMENDED_TAG_OVERRIDES,
    EpubDeepCssAnalyzer,
    clean_deep_html_content,
    clean_opf_file,
    find_opf_file,
    get_default_deep_cleanup_tag_rules,
    normalize_deep_cleanup_tag_rules,
)
from ..overlay_host import exec_dialog
from ..widgets.overlay_tab_widget import install_tab_fade
from ...core.epub_duplicate_helpers import (
    DUPLICATE_REVIEW_BLOCK_TAGS,
    analyze_duplicate_findings,
    blocks_are_equivalent,
    blocks_look_like_same_heading,
    build_tag_path,
    collect_boundary_duplicate_findings,
    collect_start_duplicate_findings,
    extract_duplicate_review_blocks,
    format_duplicate_preview_blocks,
    get_duplicate_block_text,
    get_duplicate_text_tokens,
    is_meaningful_duplicate_text,
    merge_finding_entry,
    normalize_duplicate_text,
    resolve_tag_path,
    summarize_duplicate_run,
)

# --- Вспомогательная функция (перенесена сюда, так как используется только здесь) ---
def btn(text, func):
    b = QPushButton(text)
    b.clicked.connect(func)
    return b


DEEP_CLEANUP_SETTINGS_ORG = "gemini_translator"
DEEP_CLEANUP_SETTINGS_APP = "gemini_translator"
DEEP_CLEANUP_SETTINGS_GROUP = "epub_deep_cleanup"
DEEP_CLEANUP_SETTINGS_PROFILE_VERSION = 2


def _get_deep_cleanup_qsettings():
    return QtCore.QSettings(DEEP_CLEANUP_SETTINGS_ORG, DEEP_CLEANUP_SETTINGS_APP)


def load_deep_cleanup_settings():
    default_rules = get_default_deep_cleanup_tag_rules()
    settings = _get_deep_cleanup_qsettings()
    settings.beginGroup(DEEP_CLEANUP_SETTINGS_GROUP)
    raw_tag_rules = settings.value('tag_rules_json', '', str)
    profile_version = int(settings.value('profile_version', 0) or 0)
    settings_data = {
        'remove_css': settings.value('remove_css', True, bool),
        'remove_nav': settings.value('remove_nav', False, bool),
        'remove_fonts': settings.value('remove_fonts', True, bool),
        'apply_css_styles': settings.value('apply_css_styles', True, bool),
        'tag_rules': default_rules,
    }
    settings.endGroup()

    if raw_tag_rules:
        try:
            settings_data['tag_rules'] = normalize_deep_cleanup_tag_rules(json.loads(raw_tag_rules))
        except Exception:
            settings_data['tag_rules'] = default_rules

    if profile_version < DEEP_CLEANUP_SETTINGS_PROFILE_VERSION:
        settings_data.update({
            'remove_nav': False,
            'remove_fonts': True,
            'apply_css_styles': True,
        })
        migrated_rules = dict(settings_data['tag_rules'])
        migrated_rules.update(DEEP_CLEANUP_RECOMMENDED_TAG_OVERRIDES)
        settings_data['tag_rules'] = normalize_deep_cleanup_tag_rules(migrated_rules)
    return settings_data


def save_deep_cleanup_settings(settings_data):
    normalized_rules = normalize_deep_cleanup_tag_rules(settings_data.get('tag_rules'))
    settings = _get_deep_cleanup_qsettings()
    settings.beginGroup(DEEP_CLEANUP_SETTINGS_GROUP)
    settings.setValue('remove_css', bool(settings_data.get('remove_css', True)))
    settings.setValue('remove_nav', bool(settings_data.get('remove_nav', False)))
    settings.setValue('remove_fonts', bool(settings_data.get('remove_fonts', True)))
    settings.setValue('apply_css_styles', bool(settings_data.get('apply_css_styles', True)))
    settings.setValue('profile_version', DEEP_CLEANUP_SETTINGS_PROFILE_VERSION)
    settings.setValue(
        'tag_rules_json',
        json.dumps(normalized_rules, ensure_ascii=False)
    )
    settings.endGroup()
    settings.sync()


class EpubCleanupThread(QThread):
    """
    Хирург. Выполняет точечные резекции и синхронизацию нумерации.
    """
    finished_cleanup = pyqtSignal(object, str)

    def __init__(self, virtual_epub_path, fixes_list, parent=None):
        super().__init__(parent)
        self.virtual_epub_path = virtual_epub_path
        self.fixes = fixes_list 
        self.tasks = []
        
        # Разбираем задачи
        for fix in self.fixes:
            if fix.get('type') == 'num_mismatch':
                self.tasks.append(fix)
            elif fix.get('type') == 'force_renumber_sequential':
                self.tasks.append(fix)
            elif fix.get('type') == 'br':
                self.tasks.append({'type': 'br'})
            elif fix.get('type') == 'orphans':
                self.tasks.append({'type': 'orphans'})
            elif fix.get('type') == 'attr':
                t_tag = fix.get('tag', '')
                t_attr = fix.get('attr', '')
                t_val = re.escape(fix.get('value', ''))
                
                regex_tag = re.compile(fr'(<{t_tag}\b[^>]*>)', re.IGNORECASE)
                regex_attr = re.compile(fr'\s+{t_attr}\s*=\s*["\']{t_val}["\']', re.IGNORECASE)
                
                self.tasks.append({
                    'type': 'attr',
                    'tag_re': regex_tag,
                    'attr_re': regex_attr
                })

    def run(self):
        try:
            files_processed = 0
            # Словарь замен для глобального обновления ссылок: {filename: (old_text_fragment, new_text)}
            global_link_updates = {} 
            
            temp_output_buffer = io.BytesIO()
            
            # --- ИСПРАВЛЕНИЕ 1: Надежный импорт BS4 внутри потока ---
            has_bs4 = False
            try:
                from bs4 import BeautifulSoup
                has_bs4 = True
            except ImportError:
                has_bs4 = False
            
            # Флаг сквозной нумерации
            force_renumber = any(t['type'] == 'force_renumber_sequential' for t in self.tasks)
            
            # Получаем порядок глав
            ordered_chapters = []
            if force_renumber:
                from ...utils.epub_tools import get_epub_chapter_order
                ordered_chapters = get_epub_chapter_order(self.virtual_epub_path)
            
            with open(self.virtual_epub_path, 'rb') as epub_file, \
                    zipfile.ZipFile(epub_file, 'r') as zin:
                with zipfile.ZipFile(temp_output_buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
                    
                    # 1. Читаем все файлы в память
                    all_files_content = {}
                    for item in zin.infolist():
                        all_files_content[item.filename] = zin.read(item.filename)

                    # --- ЭТАП A: Сквозная перенумерация (Force) ---
                    if force_renumber:
                        current_chapter_index = 1
                        for filename in ordered_chapters:
                            if filename not in all_files_content: continue
                            
                            try:
                                content_str = all_files_content[filename].decode('utf-8', errors='ignore')
                                
                                old_text_fragment = ""
                                new_header_text = ""
                                
                                if has_bs4:
                                    soup = BeautifulSoup(content_str, 'html.parser')
                                    header = soup.find(['h1', 'h2', 'h3'])
                                    title_tag = soup.find('title')
                                    
                                    if header:
                                        old_text_fragment = header.get_text().strip()
                                        
                                        # ВАРИАНТ 1: Если цифры уже есть — заменяем первую группу
                                        if re.search(r'\d+', old_text_fragment):
                                            new_header_text = re.sub(r'\d+', str(current_chapter_index), old_text_fragment, count=1)
                                        
                                        # ВАРИАНТ 2: Если цифр нет — добавляем номер в начало
                                        else:
                                            new_header_text = f"({current_chapter_index}) {old_text_fragment}"

                                        # Применяем изменения
                                        if new_header_text != old_text_fragment:
                                            header.string = new_header_text
                                            if title_tag:
                                                title_tag.string = new_header_text
                                            
                                            content_str = str(soup)
                                            all_files_content[filename] = content_str.encode('utf-8')
                                            
                                            global_link_updates[filename] = (old_text_fragment, new_header_text)
                                            files_processed += 1
                                            
                                current_chapter_index += 1
                                
                            except Exception as e:
                                print(f"[Renumber Error] {filename}: {e}")

                    # --- ЭТАП B: Точечная замена (num_mismatch) ---
                    elif not force_renumber:
                        for task in [t for t in self.tasks if t.get('type') == 'num_mismatch']:
                            target_file = task['file']
                            if target_file in all_files_content:
                                try:
                                    content_str = all_files_content[target_file].decode('utf-8', errors='ignore')
                                    old_fragment = task['old_fragment']
                                    new_number = str(task['new_number'])
                                    
                                    if old_fragment in content_str:
                                        def replace_in_tag(match):
                                            return match.group(0).replace(old_fragment, new_number)
                                        
                                        content_str = re.sub(r'<(h[1-6]|title)[^>]*>.*?</\1>', replace_in_tag, content_str, flags=re.DOTALL | re.IGNORECASE)
                                        
                                        all_files_content[target_file] = content_str.encode('utf-8')
                                        global_link_updates[target_file] = (old_fragment, new_number)
                                        files_processed += 1
                                except Exception as e:
                                    print(f"Error fixing mismatch in {target_file}: {e}")

                    # --- ЭТАП C: Глобальный проход (Ссылки и остальные фиксы) ---
                    for filename, content_bytes in all_files_content.items():
                        # --- ИСПРАВЛЕНИЕ 2: Инициализируем modified_content ДО проверок ---
                        modified_content = content_bytes 
                        
                        is_html = filename.lower().endswith(('.html', '.xhtml', '.htm'))
                        is_nav = filename.lower().endswith(('.ncx', 'nav.xhtml', 'toc.html')) or 'toc' in filename.lower()
                        
                        if is_html or is_nav:
                            try:
                                content_str = modified_content.decode('utf-8', errors='ignore')
                                original_str = content_str
                                
                                # 1. Обновление ссылок
                                if global_link_updates:
                                    for target_file, (old_txt, new_txt) in global_link_updates.items():
                                        target_basename = os.path.basename(target_file)
                                        if target_basename in content_str:
                                            esc_old = re.escape(old_txt)
                                            # А. HTML ссылки
                                            pattern_a = re.compile(
                                                fr'(<a\b[^>]*href=["\'][^"\']*{re.escape(target_basename)}[^"\']*["\'][^>]*>)(.*?{esc_old}.*?)(</a>)', 
                                                re.IGNORECASE | re.DOTALL
                                            )
                                            content_str = pattern_a.sub(lambda m: f"{m.group(1)}{m.group(2).replace(old_txt, new_txt)}{m.group(3)}", content_str)
                                            
                                            # Б. NCX (Table of Contents)
                                            if filename.lower().endswith('.ncx'):
                                                content_str = content_str.replace(f"<text>{old_txt}</text>", f"<text>{new_txt}</text>")

                                # 2. Остальные задачи
                                for task in self.tasks:
                                    if task['type'] == 'br' and '<br' in content_str.lower():
                                        content_str = unify_paragraphs_for_ai(content_str)
                                    elif task['type'] == 'attr':
                                        def remove_attr(match): return task['attr_re'].sub('', match.group(1))
                                        content_str = task['tag_re'].sub(remove_attr, content_str)
                                    elif task['type'] == 'orphans':
                                        if has_bs4:
                                             soup = BeautifulSoup(content_str, 'html.parser')
                                             if soup.body:
                                                new_contents = []
                                                buffer_text = []
                                                def flush_buffer():
                                                    if buffer_text:
                                                        new_p = soup.new_tag('p')
                                                        for buf_item in buffer_text: new_p.append(buf_item)
                                                        new_contents.append(new_p)
                                                        buffer_text.clear()
                                                children = list(soup.body.children)
                                                for child in children:
                                                    block_tags = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'hr', 'ul', 'ol', 'table', 'script', 'style', 'head', 'title', 'meta', 'link', 'br'] 
                                                    is_block = isinstance(child, Tag) and child.name in block_tags
                                                    is_whitespace = isinstance(child, NavigableString) and not child.strip()
                                                    if is_block:
                                                        flush_buffer()
                                                        new_contents.append(child)
                                                    elif is_whitespace and not buffer_text:
                                                        new_contents.append(child)
                                                    else:
                                                        buffer_text.append(child)
                                                flush_buffer()
                                                soup.body.clear()
                                                for item_node in new_contents: soup.body.append(item_node)
                                                content_str = str(soup)

                                if content_str != original_str:
                                    if filename not in global_link_updates:
                                        files_processed += 1
                                    modified_content = content_str.encode('utf-8')

                            except Exception as e:
                                print(f"Error processing {filename}: {e}")

                        # Теперь запись безопасна для любых типов файлов
                        zout.writestr(filename, modified_content)
            
            temp_output_buffer.seek(0)
            with open(self.virtual_epub_path, 'wb') as f:
                f.write(temp_output_buffer.getvalue())

            final_msg = f"Операция завершена.\nОбработано файлов: {files_processed}."
            if global_link_updates:
                final_msg += f"\nОбновлена нумерация для {len(global_link_updates)} глав."
            self.finished_cleanup.emit(self.virtual_epub_path, final_msg)

        except Exception as e:
            import traceback
            print(traceback.format_exc())
            self.finished_cleanup.emit(None, f"Ошибка: {e}")

  
class EpubDeepCleanupThread(QThread):
    finished_cleanup = pyqtSignal(object, str)

    def __init__(self, virtual_epub_path, options, parent=None):
        super().__init__(parent)
        self.virtual_epub_path = virtual_epub_path
        self.options = options or {}
        self.tag_rules = normalize_deep_cleanup_tag_rules(
            self.options.get('tag_rules') or get_default_deep_cleanup_tag_rules()
        )
        self.css_analyzer = EpubDeepCssAnalyzer()
        self.css_styles = {}
        self.removed_css_files = set()
        self.removed_nav_files = set()
        self.removed_font_files = set()

    def run(self):
        if not BS4_AVAILABLE:
            self.finished_cleanup.emit(None, "Для глубокой чистки нужен пакет beautifulsoup4.")
            return

        try:
            if not self._validate_epub():
                raise ValueError("Файл в памяти не похож на валидный EPUB.")

            with tempfile.TemporaryDirectory() as temp_dir:
                with open(self.virtual_epub_path, 'rb') as epub_file, \
                        zipfile.ZipFile(epub_file, 'r') as zin:
                    zin.extractall(temp_dir)

                if self.options.get('apply_css_styles'):
                    self._analyze_css_files(temp_dir)

                stats = {
                    'html': 0,
                    'css': 0,
                    'nav': 0,
                    'fonts': 0,
                }

                for root, _, files in os.walk(temp_dir):
                    for file_name in files:
                        file_path = os.path.join(root, file_name)
                        rel_path = os.path.relpath(file_path, temp_dir).replace('\\', '/')
                        lower_name = file_name.lower()

                        if self.options.get('remove_css') and lower_name.endswith('.css'):
                            self.removed_css_files.add(rel_path)
                            os.remove(file_path)
                            stats['css'] += 1
                            continue

                        if self.options.get('remove_nav') and lower_name in ('nav.xhtml', 'nav.html'):
                            self.removed_nav_files.add(rel_path)
                            os.remove(file_path)
                            stats['nav'] += 1
                            continue

                        if self.options.get('remove_fonts') and lower_name.endswith(('.ttf', '.otf', '.woff', '.woff2')):
                            self.removed_font_files.add(rel_path)
                            os.remove(file_path)
                            stats['fonts'] += 1
                            continue

                        if lower_name.endswith(('.html', '.xhtml', '.htm')):
                            self._clean_html_file(file_path)
                            stats['html'] += 1

                opf_path = find_opf_file(temp_dir)
                if opf_path:
                    clean_opf_file(
                        temp_dir,
                        opf_path,
                        self.options,
                        self.removed_css_files,
                        self.removed_nav_files,
                        self.removed_font_files,
                    )

                self._write_epub_from_dir(temp_dir, self.virtual_epub_path)

            message_lines = [
                "Глубокая чистка завершена.",
                f"HTML/XHTML обработано: {stats['html']}.",
            ]
            if self.options.get('remove_css'):
                message_lines.append(f"CSS удалено: {stats['css']}.")
            if self.options.get('remove_nav'):
                message_lines.append(f"Навигационных файлов удалено: {stats['nav']}.")
            if self.options.get('remove_fonts'):
                message_lines.append(f"Шрифтов удалено: {stats['fonts']}.")
            if self.options.get('apply_css_styles'):
                message_lines.append(f"CSS-правил перенесено в HTML: {len(self.css_styles)}.")

            self.finished_cleanup.emit(self.virtual_epub_path, "\n".join(message_lines))
        except Exception as e:
            print(traceback.format_exc())
            self.finished_cleanup.emit(None, f"Ошибка глубокой чистки: {e}")

    def _validate_epub(self):
        try:
            with open(self.virtual_epub_path, 'rb') as epub_file, \
                    zipfile.ZipFile(epub_file, 'r') as zf:
                if 'mimetype' not in zf.namelist():
                    return False
                mimetype = zf.read('mimetype').decode('utf-8', errors='ignore').strip()
                return mimetype == 'application/epub+zip'
        except Exception:
            return False

    def _analyze_css_files(self, temp_dir):
        for root, _, files in os.walk(temp_dir):
            for file_name in files:
                if not file_name.lower().endswith('.css'):
                    continue
                file_path = os.path.join(root, file_name)
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as fh:
                        css_content = fh.read()
                    self.css_styles.update(self.css_analyzer.parse_css_content(css_content))
                except Exception:
                    continue

    def _clean_html_file(self, file_path):
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as fh:
            content = fh.read()

        output = clean_deep_html_content(
            content,
            self.options,
            self.tag_rules,
            self.css_styles,
            self.css_analyzer,
        )

        with open(file_path, 'w', encoding='utf-8', errors='ignore') as fh:
            fh.write(output)

    def _write_epub_from_dir(self, source_dir, output_path):
        with zipfile.ZipFile(output_path, 'w') as zout:
            mimetype_path = os.path.join(source_dir, 'mimetype')
            if os.path.exists(mimetype_path):
                zout.write(mimetype_path, 'mimetype', compress_type=zipfile.ZIP_STORED)

            for root, _, files in os.walk(source_dir):
                for file_name in files:
                    if file_name == 'mimetype':
                        continue
                    file_path = os.path.join(root, file_name)
                    arc_name = os.path.relpath(file_path, source_dir).replace('\\', '/')
                    zout.write(file_path, arc_name, compress_type=zipfile.ZIP_DEFLATED)


class EpubHtmlSelectorDialog(QDialog):
    """
    Диалог выбора глав из EPUB с истинной многоэтапной "ленивой" инициализацией.
    Версия 5.0: Заглушка -> Скелет UI -> Preview-список -> Финальная отрисовка.
    """
    _final_epub_path = None
    
    def __init__(
        self,
        epub_filename,
        output_folder=None,
        parent=None,
        pre_selected_chapters=None,
        project_manager=None,
        previous_translated_chapter_titles=None,
    ):
        super().__init__(parent)
        self.real_epub_path = epub_filename # Сохраняем реальный путь для финального сохранения
        self.output_folder = output_folder
        self.project_manager = project_manager
        self.pre_selected_chapters = pre_selected_chapters or []
        self.previous_translated_chapter_titles = list(
            previous_translated_chapter_titles or []
        )
        
        # --- Состояние ---
        self.virtual_epub_path = None # Будет инициализирован асинхронно
        self.all_chapters = []
        self._is_loaded = False
        self._ui_is_built = False
        self.validated_chapters = set()
        self.unvalidated_chapters = set()
        self.untranslated_chapters = []
        self._size_cache = {}
        self._chapter_title_cache = {}
        self._previous_source_selection_cutoff = None
        self._current_filter_mode = self._show_all_chapters
        self._is_virtual_file_dirty = False # Флаг, что виртуальный файл был изменен
        self.deep_cleanup_settings = load_deep_cleanup_settings()
        self.deep_cleanup_tag_rules = dict(self.deep_cleanup_settings.get('tag_rules') or get_default_deep_cleanup_tag_rules())

        self._init_lazy_ui_skeleton()
    
    @staticmethod
    def get_selection(
        parent,
        epub_filename,
        output_folder=None,
        pre_selected_chapters=None,
        project_manager=None,
        previous_translated_chapter_titles=None,
    ):
        """
        Статический метод-фабрика. Создает, запускает диалог и возвращает результат.
        Это инкапсулирует всю логику работы с модальным диалогом.

        Returns:
            tuple[bool, list]: Кортеж (успех, список_выбранных_файлов).
                               `успех` будет True, если пользователь нажал "Принять".
        """
        dialog = EpubHtmlSelectorDialog(
            epub_filename=epub_filename,
            output_folder=output_folder,
            parent=parent,
            pre_selected_chapters=pre_selected_chapters,
            project_manager=project_manager,
            previous_translated_chapter_titles=previous_translated_chapter_titles,
        )
        
        result = exec_dialog(parent, dialog)
        
        if result == QDialog.DialogCode.Accepted:
            return True, dialog.get_selected_files()
        else:
            return False, []
    
    def _init_lazy_ui_skeleton(self):
        """ЭТАП 1: Создает только скелет окна с заглушкой. Выполняется мгновенно."""
        # Используем self.real_epub_path, как и в конструкторе
        self.setWindowTitle(f"Выберите главы из '{os.path.basename(self.real_epub_path)}'")
        self.setMinimumWidth(700)
        self.setMinimumHeight(500)
        main_layout = QVBoxLayout(self)

        self.loading_label = QLabel("<h2>Загрузка интерфейса…</h2>")
        self.loading_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.loading_label, 1)

        self.main_content_widget = QWidget()
        self.main_content_widget.setVisible(False)
        main_layout.addWidget(self.main_content_widget, 1)


    def _populate_full_ui(self):
        content_layout = QVBoxLayout(self.main_content_widget)
        content_layout.setContentsMargins(0,0,0,0)
        
        # --- ВЕРХНЯЯ ПАНЕЛЬ (только для фильтров и синхронизации) ---
        top_bar_widget = QWidget()
        top_bar_layout = QHBoxLayout(top_bar_widget)
        top_bar_layout.setContentsMargins(0, 0, 0, 10)
        
        self.filter_buttons_widget = QWidget()
        filter_layout = QHBoxLayout(self.filter_buttons_widget)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        btn_hide_translated = QPushButton("Только непереведенные")
        btn_hide_translated.clicked.connect(self._filter_show_untranslated)
        btn_hide_validated = QPushButton("Непроверенные")
        btn_hide_validated.clicked.connect(self._filter_show_unvalidated)
        btn_show_all = QPushButton("Показать все")
        btn_show_all.clicked.connect(self._show_all_chapters)
        filter_layout.addWidget(btn_hide_translated)
        filter_layout.addWidget(btn_hide_validated)
        filter_layout.addWidget(btn_show_all)
        top_bar_layout.addWidget(self.filter_buttons_widget)
        top_bar_layout.addStretch()
        
        self.sync_project_btn = QPushButton("🔄 Сверить проект")
        self.sync_project_btn.clicked.connect(self._run_project_sync)
        top_bar_layout.addWidget(self.sync_project_btn)
        
        if self.project_manager is None:
            self.filter_buttons_widget.setVisible(False)
            self.sync_project_btn.setVisible(False)
            
        content_layout.addWidget(top_bar_widget)
    
        # --- СПИСОК ГЛАВ ---
        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        content_layout.addWidget(self.list_widget, 1)
    
        # --- НИЖНЯЯ ПАНЕЛЬ (содержит кнопки очистки и основные кнопки) ---
        bottom_bar_layout = QHBoxLayout()
    
        
        
        
        # --- Кнопки (слева) ---
        self.cleanup_panel = QWidget()
        cleanup_layout = QHBoxLayout(self.cleanup_panel)
        cleanup_layout.setContentsMargins(0, 0, 0, 0)
        cleanup_layout.setSpacing(10)
        
        # КНОПКА АНАЛИЗА
        self.analyze_btn = QPushButton("🏥 Диагностика и Лечение")
        self.analyze_btn.setToolTip("Запустить полный анализ структуры книги на наличие мусорных тегов и атрибутов.")
        self.analyze_btn.clicked.connect(self._run_full_analysis) # <-- Новый метод

        self.deep_cleanup_btn = QPushButton("🧹 Полная чистка EPUB")
        self.deep_cleanup_btn.setToolTip(
            "Альтернативный режим: удаление CSS/nav, массовая чистка тегов и атрибутов, "
            "с опциональным переносом части CSS в HTML."
        )
        self.deep_cleanup_btn.clicked.connect(self._open_deep_cleanup_dialog)
        self.duplicate_cleanup_btn = QPushButton("🪞 Повторы в главах")
        self.duplicate_cleanup_btn.setToolTip(
            "Один инструмент с двумя режимами: поиск дублей в начале главы и на стыках/в концовках между главами."
        )
        self.duplicate_cleanup_btn.clicked.connect(self._open_duplicate_cleanup_dialog)
        
        # Кнопка бэкапа
        self.restore_backup_btn = QPushButton("롤 Восстановить оригинал")
        self.restore_backup_btn.clicked.connect(self._restore_from_backup)
        self.restore_backup_btn.setVisible(False)
    
        # Добавляем в лейаут
        cleanup_layout.addWidget(self.analyze_btn)
        cleanup_layout.addWidget(self.deep_cleanup_btn)
        cleanup_layout.addWidget(self.duplicate_cleanup_btn)
        cleanup_layout.addWidget(self.restore_backup_btn)
        
        # Панель видима по умолчанию вместе с родителем; явный setVisible(True)
        # до прикрепления layout к диалогу на мгновение показывал её отдельным окном.
        bottom_bar_layout.addWidget(self.cleanup_panel, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        
        
        
        
        
        
        
        # --- Распорка между левой и правой группой кнопок ---
        bottom_bar_layout.addStretch()
    
        # --- Основные кнопки диалога (справа) ---
        self.button_box = QDialogButtonBox()
        ok_button = QPushButton("Принять")
        self.accept_button = ok_button
        self.accept_button.setEnabled(
            not getattr(self, 'previous_translated_chapter_titles', None)
        )
        cancel_button = QPushButton("Отмена")
        self.button_box.addButton(ok_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.button_box.addButton(cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        bottom_bar_layout.addWidget(self.button_box, 0, QtCore.Qt.AlignmentFlag.AlignRight)
    
        content_layout.addLayout(bottom_bar_layout)

    def showEvent(self, event):
        if event:
            super().showEvent(event)
        
        # Запускаем инициализацию только один раз при первом показе
        if not self._is_loaded:
            self._is_loaded = True
            # Запускаем самый первый асинхронный шаг
            QtCore.QTimer.singleShot(50, self._async_initial_setup)


    def _async_stage_1_build_ui_if_needed(self):
        """ЭТАП 1: Строит UI, только если он еще не был построен."""
        if not self._ui_is_built:
            self._populate_full_ui()
            self._ui_is_built = True
    
        # "Подменяем" заглушку на готовый интерфейс
        self.loading_label.setVisible(False)
        self.main_content_widget.setVisible(True)
        
        # ДАЕМ ИНТЕРФЕЙСУ "ПРОДОХНУТЬ"
        QtWidgets.QApplication.processEvents()
    
        # И ТОЛЬКО ПОСЛЕ ЭТОГО запускаем следующий, блокирующий этап
        QtCore.QTimer.singleShot(0, self._async_stage_2_get_filelist)
    
    
    def _async_initial_setup(self):
        """
        Выполняется один раз. Строит UI (если нужно) и запускает первую загрузку данных.
        """
        # Проверяем ВАШ флаг
        if not self._ui_is_built:
            # Строим UI только если он еще не был построен
            self._populate_full_ui()
            self._ui_is_built = True # <-- Устанавливаем флаг НАВСЕГДА
    
        # "Подменяем" заглушку на готовый интерфейс
        self.loading_label.setVisible(False)
        self.main_content_widget.setVisible(True)
        QtWidgets.QApplication.processEvents()
    
        # Запускаем цепочку загрузки данных
        self._start_data_loading_chain()

    def _start_data_loading_chain(self):
        """Просто запускает _async_stage_2_get_filelist."""
        QtCore.QTimer.singleShot(0, self._async_stage_2_get_filelist)
    
    def _async_stage_2_get_filelist(self):
        """ЭТАП 2: Копирует EPUB в память и читает его структуру с учетом spine."""
        try:
            if not self.virtual_epub_path:
                 self.virtual_epub_path = os.copy_to_mem(self.real_epub_path, unique=True)
                 if not self.virtual_epub_path:
                     raise IOError("Не удалось скопировать EPUB файл в память.")
            
            # --- НАЧАЛО НОВОЙ ЛОГИКИ ---
            # Вызываем централизованную функцию, передавая ей виртуальный путь
            self.all_chapters = get_epub_chapter_order(self.virtual_epub_path)
            self._load_chapter_title_cache()
            # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

        except Exception as e:
            QMessageBox.critical(self, "Ошибка EPUB", f"Не удалось прочитать структуру файла:\n{e}")
            self.reject()
            return
        
        self._populate_list_widget_preview()
        QApplication.processEvents()
        QtCore.QTimer.singleShot(0, self._async_stage_3_load_details)
    
    def _populate_list_widget_preview(self):
        self.list_widget.clear()
        for i, file_path in enumerate(self.all_chapters):
            display_text = f"{os.path.basename(file_path)}"
            item = QListWidgetItem(display_text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, file_path)
            self._apply_chapter_item_tooltip(item, file_path)
            self.list_widget.addItem(item)
        
        if not self.pre_selected_chapters:
            self.list_widget.selectAll()
        else:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in self.pre_selected_chapters:
                    item.setSelected(True)
    
    def _async_stage_3_load_details(self):
        # Скрываем кнопки действий по умолчанию
        self.restore_backup_btn.setVisible(False)
        self.suspicious_style = None
    
        # Быстрая проверка бэкапа
        backup_path = self.real_epub_path + ".backup"
        if os.path.exists(backup_path):
            self.restore_backup_btn.setVisible(True)
    
        # Получение размеров (используем кэш или быстрое чтение)
        if self.project_manager:
            self._size_cache = get_epub_chapter_sizes_with_cache(
                self.project_manager,
                self.real_epub_path,
                task_size_unit=TASK_SIZE_UNIT_CHARS,
            )
        else:
            self._size_cache = {}
            try:
                # ЭВРИСТИЧЕСКИЙ ПОДСЧЕТ СИМВОЛОВ
                # Вместо того чтобы читать ВСЕ файлы (долго) или показывать байты (неточно),
                # мы берем медианный файл, считаем его реальную длину в символах,
                # вычисляем коэффициент сжатия/кодировки и применяем ко всем.
                
                with open(self.virtual_epub_path, "rb") as epub_file, \
                        zipfile.ZipFile(epub_file, "r") as zf:
                    # 1. Сначала собираем сырые размеры в байтах
                    temp_byte_sizes = []
                    for name in self.all_chapters:
                        info = zf.getinfo(name)
                        temp_byte_sizes.append((name, info.file_size))
                    
                    if temp_byte_sizes:
                        # 2. Находим медианный файл (середина списка по размеру)
                        # Сортируем по размеру, чтобы найти "типичного представителя"
                        sorted_by_size = sorted(temp_byte_sizes, key=lambda x: x[1])
                        median_idx = len(sorted_by_size) // 2
                        median_name, median_bytes = sorted_by_size[median_idx]
                        
                        # 3. Вычисляем коэффициент (Char/Byte Ratio) на основе медианы
                        ratio = 1.0 # fallback
                        if median_bytes > 0:
                            try:
                                content_sample = zf.read(median_name).decode('utf-8', errors='ignore')
                                chars_count = len(content_sample)
                                ratio = chars_count / median_bytes
                            except Exception:
                                pass # Оставляем ratio = 1.0
                        
                        # 4. Применяем коэффициент ко всем файлам
                        for name, b_size in temp_byte_sizes:
                            # Округляем до целого
                            estimated_chars = max(1, int(b_size * ratio)) if b_size > 0 else 0
                            self._size_cache[name] = estimated_chars

            except Exception as e:
                print(f"Ошибка чтения размеров: {e}")

        # Обновление статусов проекта
        if self.project_manager:
            self._update_chapter_statuses()
            if self.untranslated_chapters and not self.pre_selected_chapters:
                self._current_filter_mode = self._filter_show_untranslated
            else:
                self._current_filter_mode = self._show_all_chapters
        else:
            self.untranslated_chapters = self.all_chapters
            self._current_filter_mode = self._show_all_chapters

        # Обновление списка
        selected_files_before_update = self.get_selected_files()
        self._current_filter_mode()
        
        # Если в проекте уже есть переведенная часть прошлой пачки, новый
        # выбор начинается строго после нее. Восстановление старого выделения
        # иначе снова отметило бы уже пройденные главы.
        if getattr(self, '_previous_source_selection_cutoff', None) is None:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(QtCore.Qt.ItemDataRole.UserRole) in selected_files_before_update:
                    item.setSelected(True)

    def _populate_list_widget(self, chapters_to_show):
        self.list_widget.clear()
        for i, file_path in enumerate(chapters_to_show):
            size_chars = self._size_cache.get(file_path, 0)
            
            display_text = f"{os.path.basename(file_path)} ({size_chars:,} симв.)"
            item = QListWidgetItem(display_text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, file_path)
            self._apply_chapter_item_tooltip(item, file_path)
            
            if file_path in self.validated_chapters:
                item.setBackground(QtGui.QColor(46, 75, 62, 80))
            elif file_path in self.unvalidated_chapters:
                item.setBackground(QtGui.QColor(58, 75, 95, 80))
            
            self.list_widget.addItem(item)

    @staticmethod
    def _extract_h1_title(html_content):
        # Заголовок нужен только для подсказки; полный BS4-парсинг каждой
        # главы стоил секунды на больших книгах, поэтому берём h1 регулярным
        # выражением (сверено с BS4 на реальных книгах — результат совпадает).
        if not html_content:
            return ""

        match = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", str(html_content), re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        raw_title = re.sub(r"<(?:br|hr)\b[^>]*>", " ", match.group(1), flags=re.IGNORECASE)
        raw_title = re.sub(r"<[^>]+>", "", raw_title)
        return re.sub(r"\s+", " ", html_lib.unescape(raw_title)).strip()

    def _load_chapter_title_cache(self):
        self._chapter_title_cache = {}
        self._title_scan_index = None
        if (
            getattr(self, 'previous_translated_chapter_titles', None)
            and hasattr(self, 'accept_button')
        ):
            self.accept_button.setEnabled(False)
        if not self.virtual_epub_path or not self.all_chapters:
            self._finish_chapter_title_scan()
            return
        # Ленивая догрузка: не задерживаем появление списка распаковкой и
        # декодированием ВСЕХ глав книги — сканируем батчами через очередь
        # событий, подсказки дозаполняются по мере готовности.
        self._title_scan_index = 0
        QtCore.QTimer.singleShot(0, self._scan_chapter_titles_batch)

    def _scan_chapter_titles_batch(self):
        BATCH = 100
        start = getattr(self, '_title_scan_index', None)
        if start is None or not self.virtual_epub_path:
            return
        chapters = self.all_chapters[start:start + BATCH]
        if not chapters:
            self._finish_chapter_title_scan()
            return

        scanned = {}
        try:
            with open(self.virtual_epub_path, "rb") as epub_file, \
                    zipfile.ZipFile(epub_file, "r") as zf:
                for file_path in chapters:
                    try:
                        content = zf.read(file_path).decode("utf-8", errors="ignore")
                    except Exception as e:
                        print(f"[WARN] Failed to read chapter title for '{file_path}': {e}")
                        continue

                    title = self._extract_h1_title(content)
                    if title:
                        scanned[file_path] = title
        except Exception as e:
            print(f"[WARN] Failed to load EPUB chapter titles: {e}")
            self._finish_chapter_title_scan()
            return

        self._chapter_title_cache.update(scanned)
        self._title_scan_index = start + BATCH
        if scanned:
            self._refresh_tooltips_for_paths(set(scanned))
        QtCore.QTimer.singleShot(0, self._scan_chapter_titles_batch)

    def _finish_chapter_title_scan(self):
        self._title_scan_index = None
        self._apply_previous_source_title_cutoff()
        if hasattr(self, 'accept_button'):
            self.accept_button.setEnabled(True)

    def _refresh_tooltips_for_paths(self, paths):
        list_widget = getattr(self, 'list_widget', None)
        if list_widget is None:
            return
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item is None:
                continue
            file_path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if file_path in paths:
                self._apply_chapter_item_tooltip(item, file_path)

    def _apply_chapter_item_tooltip(self, item, file_path):
        chapter_title = (self._chapter_title_cache.get(file_path) or "").strip()
        if chapter_title:
            item.setToolTip(f"H1: {chapter_title}\n{file_path}")
        else:
            item.setToolTip(file_path)

    @staticmethod
    def _normalize_chapter_title(title):
        normalized = html_lib.unescape(str(title or ""))
        normalized = unicodedata.normalize("NFKC", normalized)
        return re.sub(r"\s+", " ", normalized).strip().casefold()

    @classmethod
    def find_previous_title_cutoff(cls, current_titles, previous_titles):
        """Находит последнее однозначное пересечение новой и старой пачек."""
        current = [cls._normalize_chapter_title(title) for title in current_titles or []]
        previous = []
        for title in previous_titles or []:
            normalized_title = cls._normalize_chapter_title(title)
            if normalized_title:
                previous.append(normalized_title)
        if not current or not previous:
            return None

        # Ищем с конца прошлой пачки: если новый файл содержит только часть
        # старого хвоста, границей станет последняя реально повторившаяся глава.
        for previous_index in range(len(previous) - 1, -1, -1):
            target_title = previous[previous_index]
            candidates = [
                index for index, title in enumerate(current)
                if title == target_title
            ]
            if not candidates:
                continue
            if len(candidates) == 1:
                return candidates[0]

            # Одинаковые названия разрешаем по предшествующему контексту.
            # При равном результате границу не ставим, чтобы не пропустить текст.
            scores = {}
            for candidate in candidates:
                score = 0
                old_index = previous_index
                new_index = candidate
                while (
                    old_index >= 0
                    and new_index >= 0
                    and previous[old_index] == current[new_index]
                ):
                    score += 1
                    old_index -= 1
                    new_index -= 1
                scores[candidate] = score

            best_score = max(scores.values())
            best_candidates = [
                candidate for candidate, score in scores.items()
                if score == best_score
            ]
            if len(best_candidates) == 1:
                return best_candidates[0]
            return None

        return None

    def _apply_previous_source_title_cutoff(self):
        previous_titles = getattr(
            self,
            'previous_translated_chapter_titles',
            None,
        )
        if not previous_titles:
            return False

        current_titles = [
            self._chapter_title_cache.get(chapter, "")
            for chapter in self.all_chapters
        ]
        cutoff = self.find_previous_title_cutoff(
            current_titles,
            previous_titles,
        )
        if cutoff is None:
            return False

        self._previous_source_selection_cutoff = cutoff
        if self._ui_is_built and self.list_widget.count():
            self._current_filter_mode()
        return True

    def _select_chapters_after_previous_source(self):
        cutoff = getattr(self, '_previous_source_selection_cutoff', None)
        if cutoff is None:
            return False

        chapters_after_cutoff = set(self.all_chapters[cutoff + 1:])
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            file_path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            item.setSelected(file_path in chapters_after_cutoff)
        return True
    
    def _filter_show_untranslated(self):
        self._current_filter_mode = self._filter_show_untranslated
        self._populate_list_widget(self.untranslated_chapters)
        if not self._select_chapters_after_previous_source():
            self.list_widget.selectAll()

    def _filter_show_unvalidated(self):
        self._current_filter_mode = self._filter_show_unvalidated
        combined_list = sorted(set(self.untranslated_chapters + list(self.unvalidated_chapters)), key=extract_number_from_path)
        self._populate_list_widget(combined_list)
        if not self._select_chapters_after_previous_source():
            self.list_widget.selectAll()

    def _show_all_chapters(self):
        self._current_filter_mode = self._show_all_chapters
        self._populate_list_widget(self.all_chapters)
        if self._select_chapters_after_previous_source():
            return
        if not self.pre_selected_chapters:
            self.list_widget.selectAll()
    
    def _restore_from_backup(self):
        backup_path = self.real_epub_path + ".backup"
            
        if not os.path.exists(backup_path):
            QMessageBox.warning(self, "Ошибка", "Не найден файл резервной копии для восстановления.")
            return
    
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Подтверждение восстановления")
        msg_box.setText("Вы уверены, что хотите восстановить оригинальный файл из резервной копии?")
        msg_box.setInformativeText("Текущий (очищенный) файл будет перезаписан. Это действие необратимо.")
        yes_button = msg_box.addButton("Да, восстановить", QMessageBox.ButtonRole.YesRole)
        msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        
        if msg_box.exec() and msg_box.clickedButton() == yes_button:
            try:
                # 1. ДИСК: Восстанавливаем физический файл из бэкапа
                os.replace(backup_path, self.real_epub_path)
                
                # 2. ПАМЯТЬ: Принудительно синхронизируем виртуальный файл
                # Мы читаем свежие байты с диска...
                with open(self.real_epub_path, 'rb') as f_disk:
                    restored_data = f_disk.read()
                
                # ...и перезаписываем ими существующий виртуальный файл.
                # Путь self.virtual_epub_path остается тем же, меняется только контент внутри.
                with open(self.virtual_epub_path, 'wb') as f_mem:
                    f_mem.write(restored_data)
                
                self._is_virtual_file_dirty = False
                
                QMessageBox.information(self, "Успех", "Оригинальный файл восстановлен на диске и в памяти.")
                
                # 3. ИНТЕРФЕЙС: Перезагружаем список, чтобы показать старые (неочищенные) размеры
                self._reload_dialog_content()
                
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось восстановить файл: {e}")
    
    def get_selected_files(self):
        return [item.data(QtCore.Qt.ItemDataRole.UserRole) for item in self.list_widget.selectedItems()]
    
    def _run_full_analysis(self):
        """Запуск анализатора."""
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Диагностика")
        self.wait_dialog.setText("Сканирование пациента (EPUB)...\nИзучение истории болезни тегов и атрибутов.")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.analysis_thread = EpubAnalysisThread(self.virtual_epub_path, self.all_chapters)
        self.analysis_thread.analysis_finished.connect(self._on_full_analysis_finished)
        self.analysis_thread.start()

        show_when_slow(self.wait_dialog)

    def _on_full_analysis_finished(self, issues):
        """Анализ завершен. Показываем результаты."""
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        # Даже если проблем нет, даем возможность открыть инструменты
        if not issues:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Здоров!")
            msg_box.setText(
                "Критических аномалий (мусорных тегов) не обнаружено.\n\n"
                "Однако вы можете открыть инструменты для ручных операций\n"
                "(например, для сквозной перенумерации глав)."
            )
            msg_box.setIcon(QMessageBox.Icon.Information)
            
            btn_open = msg_box.addButton("Открыть инструменты", QMessageBox.ButtonRole.AcceptRole)
            btn_close = msg_box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
            
            msg_box.exec()
            
            if msg_box.clickedButton() != btn_open:
                return

        # Открываем диалог (теперь он покажет ручные опции даже без issues)
        dialog = EpubCleanupOptionsDialog(issues, self)
        if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
            fixes = dialog.get_fixes()
            if fixes:
                self._run_surgical_cleanup(fixes)

    def _run_surgical_cleanup(self, fixes):
        """Запуск хирурга."""
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Операция")
        self.wait_dialog.setText("Идет резекция выбранных тканей...\nПожалуйста, подождите.")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.cleanup_thread = EpubCleanupThread(self.virtual_epub_path, fixes)
        self.cleanup_thread.finished_cleanup.connect(self._on_surgical_cleanup_finished)
        self.cleanup_thread.start()
        self.wait_dialog.show()

    def _open_deep_cleanup_dialog(self):
        if not BS4_AVAILABLE:
            QMessageBox.warning(
                self,
                "Недоступно",
                "Для глубокой чистки нужен пакет beautifulsoup4. Сейчас он не найден."
            )
            return

        dialog = EpubDeepCleanupOptionsDialog(self.deep_cleanup_settings, self.deep_cleanup_tag_rules, self)
        if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
            options = dialog.get_options()
            self.deep_cleanup_settings = dict(options)
            self.deep_cleanup_tag_rules = dict(options.get('tag_rules') or get_default_deep_cleanup_tag_rules())
            self._run_deep_cleanup(options)

    def _open_duplicate_cleanup_dialog(self):
        if not BS4_AVAILABLE:
            QMessageBox.warning(
                self,
                "Недоступно",
                "Для поиска повторов нужен пакет beautifulsoup4. Сейчас он не найден."
            )
            return

        if not self.all_chapters:
            QMessageBox.information(self, "Нет данных", "Список глав еще не загружен.")
            return

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Поиск повторов")
        self.wait_dialog.setText(
            "Анализируются главы EPUB...\n"
            "Ищем повторы в начале главы и на стыках/в концовках между главами."
        )
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.duplicate_analysis_thread = EpubDuplicateAnalysisThread(
            self.virtual_epub_path,
            self.all_chapters,
            self,
        )
        self.duplicate_analysis_thread.analysis_finished.connect(self._on_duplicate_analysis_finished)
        self.duplicate_analysis_thread.start()
        self.wait_dialog.show()

    def _on_duplicate_analysis_finished(self, analysis_data):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        dialog = EpubDuplicateReviewDialog(analysis_data or {}, self)
        if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
            selected_findings = dialog.get_selected_findings()
            if selected_findings:
                self._run_duplicate_cleanup(selected_findings)

    def _run_duplicate_cleanup(self, selected_findings):
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Удаление повторов")
        self.wait_dialog.setText(
            "Удаляются выбранные повторы...\n"
            "Изменения применяются к EPUB в памяти, а затем синхронизируются на диск."
        )
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.duplicate_cleanup_thread = EpubDuplicateCleanupThread(
            self.virtual_epub_path,
            selected_findings,
            self,
        )
        self.duplicate_cleanup_thread.finished_cleanup.connect(self._on_duplicate_cleanup_finished)
        self.duplicate_cleanup_thread.start()
        self.wait_dialog.show()

    def _run_deep_cleanup(self, options):
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Полная чистка EPUB")
        self.wait_dialog.setText(
            "Идет глубокая очистка книги...\n"
            "Удаляются служебные стили, чистятся HTML/XHTML и пересобирается EPUB."
        )
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.deep_cleanup_thread = EpubDeepCleanupThread(self.virtual_epub_path, options)
        self.deep_cleanup_thread.finished_cleanup.connect(self._on_deep_cleanup_finished)
        self.deep_cleanup_thread.start()
        self.wait_dialog.show()

    def _on_surgical_cleanup_finished(self, virtual_path, message):
        self._finalize_cleanup_result(virtual_path, message)

    def _on_deep_cleanup_finished(self, virtual_path, message):
        self._finalize_cleanup_result(virtual_path, message)

    def _on_duplicate_cleanup_finished(self, virtual_path, message):
        self._finalize_cleanup_result(virtual_path, message)

    def _finalize_cleanup_result(self, virtual_path, message):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()
    
        if not virtual_path:
            QMessageBox.critical(self, "Остановка сердца", message)
            return
    
        try:
            real_target_path = self.real_epub_path
            backup_path = real_target_path + ".backup"
            if not os.path.exists(backup_path) and os.path.exists(real_target_path):
                shutil.copy2(real_target_path, backup_path)
                print(f"[INFO] Создан бэкап: {backup_path}")
            
            success = os.copy_from_mem(virtual_path, real_target_path)
            
            if not success:
                raise IOError("Не удалось синхронизировать память с диском.")
            
            self._is_virtual_file_dirty = False
            self.restore_backup_btn.setVisible(True)
            
            QMessageBox.information(self, "Выписка", f"{message}\n\nПамять и диск синхронизированы.")
            
            self._reload_dialog_content()

        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения", f"Лечение прошло в памяти, но сбой при записи на диск:\n{e}")
        
    def _run_project_sync(self):
        if not self.project_manager: return
        from ...utils.project_migrator import ProjectMigrator, SyncThread
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Синхронизация")
        self.wait_dialog.setText("Идет анализ проекта…")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)
        migrator = ProjectMigrator(self.output_folder, self.real_epub_path, self.project_manager)
        self.sync_thread = SyncThread(migrator, parent_widget=self)
        self.sync_thread.finished_sync.connect(self._on_sync_finished)
        self.sync_thread.start()
        self.wait_dialog.show()

    def _on_sync_finished(self, is_project_ready, message):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()
        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            return
        self.project_manager.reload_data_from_disk()
        self._update_chapter_statuses()
        self._current_filter_mode()
        QMessageBox.information(self, "Синхронизация", message)

    def _cleanup_virtual_epub_path(self):
        path = self.virtual_epub_path
        if not path:
            return

        self.virtual_epub_path = None
        self._is_virtual_file_dirty = False
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"[WARN] Failed to cleanup virtual EPUB path '{path}': {e}")

    def _update_chapter_statuses(self):
        if not self.project_manager:
            self.untranslated_chapters = self.all_chapters
            return
        self.validated_chapters.clear()
        self.unvalidated_chapters.clear()
        self.untranslated_chapters.clear()
        try:
            full_translation_map = self.project_manager.get_full_map()
        except Exception as e:
            print(f"[ERROR] Не удалось получить карту проекта: {e}")
            self.untranslated_chapters = self.all_chapters
            return
        for chapter in self.all_chapters:
            if chapter in full_translation_map:
                versions = full_translation_map[chapter]
                if '_validated.html' in versions:
                    self.validated_chapters.add(chapter)
                else:
                    self.unvalidated_chapters.add(chapter)
            else:
                self.untranslated_chapters.append(chapter)


    def _reload_dialog_content(self):
        """
        Правильная перезагрузка: сбрасывает данные, показывает заглушку
        и перезапускает ПОЛНУЮ безопасную цепочку, начиная с этапа 1.
        """
        # Сбрасываем только данные
        self.all_chapters = []
        self.validated_chapters.clear()
        self.unvalidated_chapters.clear()
        self.untranslated_chapters = []
        self._size_cache = {}
        self._chapter_title_cache = {}
        self._previous_source_selection_cutoff = None
        self._title_scan_index = None  # останавливает цепочку батч-скана заголовков

        # Показываем заглушку
        self.main_content_widget.setVisible(False)
        self.loading_label.setVisible(True)
        QtWidgets.QApplication.processEvents() # Гарантируем, что заглушка появится
    
        # Перезапускаем всю цепочку с самого начала.
        # _async_stage_1_build_ui_if_needed проверит флаг _ui_is_built
        # и не будет перестраивать UI, а просто перейдет к загрузке данных.
        QtCore.QTimer.singleShot(0, self._async_stage_1_build_ui_if_needed)
    
    def accept(self):
        """
        Просто закрывает диалог с результатом 'Accepted'.
        Вся грязная работа по сохранению теперь делается сразу после лечения.
        """
        # На всякий случай оставим проверку, если вдруг мы изменим логику в будущем,
        # но сейчас при лечении _is_virtual_file_dirty сбрасывается в False.
        if self._is_virtual_file_dirty:
            # Эта ветка сработает только если мы добавим какие-то другие
            # манипуляции, не связанные с мгновенным лечением.
            try:
                os.copy_from_mem(self.virtual_epub_path, self.real_epub_path)
            except Exception as e:
                print(f"[WARN] Ошибка финализации при закрытии: {e}")
        try:
            super().accept()
        finally:
            self._cleanup_virtual_epub_path()

    def reject(self):
        try:
            super().reject()
        finally:
            self._cleanup_virtual_epub_path()

    def closeEvent(self, event):
        self._cleanup_virtual_epub_path()
        super().closeEvent(event)

class TranslatedChaptersManagerDialog(QDialog):
    COL_INCLUDE = 0
    COL_NUMBER = 1
    COL_SOURCE = 2
    COL_FILE = 3
    """Диалог для управления переведенными главами и создания EPUB."""
    # --- ИЗМЕНЕНИЕ 1: Обновляем конструктор ---
    def __init__(self, translated_folder, parent=None, original_epub_path=None, project_manager=None):
        super().__init__(parent)
        self.translated_folder = translated_folder
        self.original_epub_path = original_epub_path
        
        # --- Используем переданный менеджер или создаем новый, если он не был передан ---
        self.project_manager = project_manager if project_manager else TranslationProjectManager(self.translated_folder)
        
        self.chapters_data = []
        self.sort_by_last_number_checkbox = None # <<< ДОБАВЬТЕ ЭТУ СТРОКУ
        self.setWindowTitle("Менеджер EPUB")
        self._checkbox_bulk_change = False
        self._last_checkbox_row = None
        self.setMinimumSize(800, 600)
        self._fill_generation = 0
        self._fill_in_progress = False
        self.init_ui()
        if self.original_epub_path:
            self.original_file_label.setText(os.path.basename(self.original_epub_path))
        # Загрузка глав отложена: карточка появляется мгновенно, таблица
        # заполняется следом (чанками — см. _smart_update_table).
        QtCore.QTimer.singleShot(0, self.load_chapters)

    def init_ui(self):
        self._structure_modified = False
        # Иконки версий считаются один раз: standardIcon на каждую из 500+
        # строк — это была половина времени построения таблицы.
        self._version_file_icon = self.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_FileIcon
        )
        self._version_validated_icon = self.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton
        )
        # --- Основной вертикальный лейаут ---
        main_layout = QVBoxLayout(self)
        
        # Заголовок
        header = QLabel("Управление структурой книги")
        header.setStyleSheet("font-weight: bold; font-size: 14px;")
        main_layout.addWidget(header)

        # --- Блок настроек сортировки (скрытый чекбокс) ---
        sort_options_layout = QHBoxLayout()
        self.sort_by_last_number_checkbox = QtWidgets.QCheckBox("Сортировать по последнему числу в имени")
        self.sort_by_last_number_checkbox.setToolTip("Использовать для сортировки последнее число в имени файла (например, 666 из '500_666.xhtml').")
        self.sort_by_last_number_checkbox.setVisible(False)
        self.sort_by_last_number_checkbox.toggled.connect(self.load_chapters)
        sort_options_layout.addStretch()
        sort_options_layout.addWidget(self.sort_by_last_number_checkbox)
        main_layout.addLayout(sort_options_layout)

        # --- Основная рабочая область (Таблица + Кнопки управления порядком) ---
        workspace_layout = QHBoxLayout()
        
        # 1. Таблица
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["№", "Оригинал / Тип", "Файл перевода"])
        header = self.table.horizontalHeader()
        self.table.setHorizontalHeaderItem(self.COL_INCLUDE, QTableWidgetItem("Вкл."))
        self.table.setHorizontalHeaderItem(self.COL_NUMBER, QTableWidgetItem("№"))
        self.table.setHorizontalHeaderItem(self.COL_SOURCE, QTableWidgetItem("Оригинал / Тип"))
        self.table.setHorizontalHeaderItem(self.COL_FILE, QTableWidgetItem("Файл перевода"))
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.itemSelectionChanged.connect(self._update_preview_button_state)
        self.table.cellDoubleClicked.connect(self._preview_selected_chapter)
        workspace_layout.addWidget(self.table)

        # 2. Панель кнопок перемещения (справа)
        self.order_buttons_layout = QVBoxLayout()
        self.order_buttons_layout.addStretch()
        
        self.btn_move_top = QPushButton("⬆⬆")
        self.btn_move_top.setToolTip("Переместить в самый верх")
        self.btn_move_top.setFixedWidth(40)
        self.btn_move_top.clicked.connect(lambda: self._move_row_extreme(top=True))
        
        self.btn_move_up = QPushButton("⬆")
        self.btn_move_up.setToolTip("Переместить выше")
        self.btn_move_up.setFixedWidth(40)
        self.btn_move_up.clicked.connect(lambda: self._move_row(-1))
        
        self.btn_move_down = QPushButton("⬇")
        self.btn_move_down.setToolTip("Переместить ниже")
        self.btn_move_down.setFixedWidth(40)
        self.btn_move_down.clicked.connect(lambda: self._move_row(1))
        
        self.btn_move_bottom = QPushButton("⬇⬇")
        self.btn_move_bottom.setToolTip("Переместить в самый низ")
        self.btn_move_bottom.setFixedWidth(40)
        self.btn_move_bottom.clicked.connect(lambda: self._move_row_extreme(top=False))

        self.order_buttons_layout.addWidget(self.btn_move_top)
        self.order_buttons_layout.addWidget(self.btn_move_up)
        self.order_buttons_layout.addWidget(self.btn_move_down)
        self.order_buttons_layout.addWidget(self.btn_move_bottom)
        self.order_buttons_layout.addStretch()
        
        workspace_layout.addLayout(self.order_buttons_layout)
        main_layout.addLayout(workspace_layout)

        # --- Панель действий с файлами ---
        actions_layout = QHBoxLayout()
        self.btn_add_custom = QPushButton("➕ Добавить файлы…")
        self.btn_add_custom.clicked.connect(self.add_custom_chapters)
        
        self.btn_replace = QPushButton("🔄 Заменить выбранный…")
        self.btn_replace.clicked.connect(self.replace_selected_file)
        
        self.btn_delete = QPushButton("❌ Удалить выбранный")
        self.btn_delete.clicked.connect(self.delete_selected_file)
        self.btn_duplicate_cleanup = QPushButton("🪞 Повторы в главах")
        self.btn_duplicate_cleanup.clicked.connect(self._open_duplicate_cleanup_for_translated_files)
        self.btn_preview = QPushButton("Редактор главы")
        self.btn_preview.clicked.connect(self._preview_selected_chapter)
        self.btn_preview.setEnabled(False)
        
        actions_layout.addWidget(self.btn_add_custom)
        actions_layout.addWidget(self.btn_replace)
        actions_layout.addWidget(self.btn_delete)
        actions_layout.addWidget(self.btn_duplicate_cleanup)
        actions_layout.addWidget(self.btn_preview)
        actions_layout.addStretch()
        self.btn_check_all = QPushButton("Выбрать все")
        self.btn_check_all.clicked.connect(lambda: self._set_all_chapters_checked(True))
        self.btn_uncheck_all = QPushButton("Снять все")
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all_chapters_checked(False))
        actions_layout.addWidget(self.btn_check_all)
        actions_layout.addWidget(self.btn_uncheck_all)
        main_layout.addLayout(actions_layout)
        self.selection_hint_label = QLabel("Галочки определяют, какие главы войдут в сборку. Shift по галочке выделяет диапазон.")
        self.selection_hint_label.setStyleSheet(f"color: {theme_manager.color('text_secondary')}; font-style: italic;")
        main_layout.addWidget(self.selection_hint_label)

        # --- Группа выбора режима ---
        mode_group = QGroupBox("Режим сборки")
        mode_layout = QVBoxLayout(mode_group)
        
        self.create_new_radio = QtWidgets.QRadioButton("Создать новый EPUB с нуля")
        self.create_new_radio.setToolTip("Позволяет менять порядок глав и добавлять новые файлы.")
        
        self.update_original_radio = QtWidgets.QRadioButton("Обновить оригинальный EPUB")
        self.update_original_radio.setToolTip("Сохраняет оригинальную структуру. Доп. файлы будут скрыты.\nАвтоматически обновляет заголовки глав на основе <h1> в переводе.")
        
        self.update_mode_info = QLabel("ℹ В режиме обновления порядок глав фиксирован оригиналом.\nЗаголовки в оглавлении будут заменены на найденные в тексте перевода (теги h1).")
        self.update_mode_info.setStyleSheet(f"color: {theme_manager.color('text_secondary')}; font-style: italic; margin-left: 20px;")
        self.update_mode_info.setVisible(False)

        # Виджет выбора оригинала
        self.original_file_widget = QWidget()
        original_file_layout = QHBoxLayout(self.original_file_widget)
        original_file_layout.setContentsMargins(20, 0, 0, 0)
        self.select_original_btn = QPushButton("Выбрать оригинал…")
        self.select_original_btn.clicked.connect(self._select_original_epub)
        self.original_file_label = QLabel("<не выбран>")
        original_file_layout.addWidget(self.select_original_btn)
        original_file_layout.addWidget(self.original_file_label, 1)
        self.original_file_widget.setVisible(False)

        mode_layout.addWidget(self.create_new_radio)
        mode_layout.addWidget(self.update_original_radio)
        mode_layout.addWidget(self.update_mode_info)
        mode_layout.addWidget(self.original_file_widget)
        main_layout.addWidget(mode_group)

        # --- Метаданные и Обложка (только для New) ---
        self.metadata_group = QGroupBox("Метаданные и Обложка")
        meta_main_layout = QVBoxLayout(self.metadata_group)
        
        # Форма с полями
        form_layout = QFormLayout()
        self.title_edit = QtWidgets.QLineEdit(os.path.basename(self.translated_folder))
        self.author_edit = QtWidgets.QLineEdit("Unknown")
        form_layout.addRow("Название:", self.title_edit)
        form_layout.addRow("Автор:", self.author_edit)
        meta_main_layout.addLayout(form_layout)
        
        # Управление обложкой
        cover_layout = QHBoxLayout()
        cover_layout.addWidget(QLabel("Обложка:"))
        
        self.cover_mode_original = QtWidgets.QRadioButton("Из оригинала")
        self.cover_mode_original.setChecked(True)
        self.cover_mode_custom = QtWidgets.QRadioButton("Свой файл")
        
        self.btn_select_cover = QPushButton("Выбрать...")
        self.btn_select_cover.setEnabled(False)
        self.btn_select_cover.clicked.connect(self._select_custom_cover)
        
        self.lbl_cover_status = QLabel("(Будет найдена автом.)")
        self.lbl_cover_status.setStyleSheet(f"color: {theme_manager.color('text_secondary')};")
        
        self.custom_cover_path = None
        
        # Логика переключения кнопок обложки
        self.cover_mode_original.toggled.connect(lambda ch: self._update_cover_ui())
        self.cover_mode_custom.toggled.connect(lambda ch: self._update_cover_ui())
        
        cover_layout.addWidget(self.cover_mode_original)
        cover_layout.addWidget(self.cover_mode_custom)
        cover_layout.addWidget(self.btn_select_cover)
        cover_layout.addWidget(self.lbl_cover_status)
        cover_layout.addStretch()
        
        meta_main_layout.addLayout(cover_layout)
        main_layout.addWidget(self.metadata_group)

        # Кнопки запуска и закрытия
        self.create_epub_btn = QPushButton("🚀 Собрать EPUB")
        self.create_epub_btn.setStyleSheet(f"background-color: {theme_manager.color('success')}; color: {theme_manager.color('accent_text')}; font-weight: bold; padding: 5px;")
        self.create_epub_btn.clicked.connect(self.create_epub)
        bottom_buttons = QHBoxLayout()
        bottom_buttons.addWidget(self.create_epub_btn, 1)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)
        bottom_buttons.addWidget(self.close_btn)
        main_layout.addLayout(bottom_buttons)

        # --- Логика переключений режимов ---
        self.create_new_radio.toggled.connect(self._on_mode_toggled)
        self.update_original_radio.toggled.connect(self._on_mode_toggled)
        
        # Устанавливаем дефолт
        self.create_new_radio.setChecked(True)


    def _on_mode_toggled(self):
        """Переключает видимость элементов и фильтрует таблицу в зависимости от режима."""
        is_create_new = self.create_new_radio.isChecked()
        is_update = self.update_original_radio.isChecked()

        # 1. Видимость виджетов
        self.metadata_group.setEnabled(is_create_new)
        self.original_file_widget.setVisible(is_update)
        self.update_mode_info.setVisible(is_update)
        
        # Кнопки модификации структуры доступны только в "Создать новый"
        self.btn_add_custom.setEnabled(is_create_new)
        self.btn_move_top.setEnabled(is_create_new)
        self.btn_move_up.setEnabled(is_create_new)
        self.btn_move_down.setEnabled(is_create_new)
        self.btn_move_bottom.setEnabled(is_create_new)

        if is_update:
            self._enforce_original_structure_view()
        else:
            self._restore_full_view()

    def _enforce_original_structure_view(self):
        """Режим обновления: скрываем кастомные файлы. Перезагружаем только если порядок нарушен."""
        
        # 1. Проверяем, нужно ли восстанавливать порядок.
        # Мы перезагружаем таблицу ТОЛЬКО если:
        # А) Пользователь руками двигал/добавлял/удалял строки (self._structure_modified)
        # Б) Включена "альтернативная сортировка" чекбоксом (значит текущий порядок неканоничный)
        need_reload = self._structure_modified or (self.sort_by_last_number_checkbox and self.sort_by_last_number_checkbox.isChecked())
        
        if self.original_epub_path and need_reload:
             # Если перезагрузка нужна — отключаем чекбокс сортировки, чтобы load_chapters загрузил канон
             if self.sort_by_last_number_checkbox:
                 self.sort_by_last_number_checkbox.blockSignals(True)
                 self.sort_by_last_number_checkbox.setChecked(False)
                 self.sort_by_last_number_checkbox.blockSignals(False)
             
             self.load_chapters()

        # 2. Блокируем отрисовку, чтобы цикл скрытия 2500 строк прошел мгновенно
        self.table.setUpdatesEnabled(False) 
        try:
            for i in range(self.table.rowCount()):
                item = self.table.item(i, self.COL_SOURCE)
                original_path = item.data(QtCore.Qt.ItemDataRole.UserRole)
                
                # Если это кастомный файл (нет оригинального пути) -> скрываем
                if not original_path:
                    self.table.setRowHidden(i, True)
                else:
                    self.table.setRowHidden(i, False)
        finally:
            self.table.setUpdatesEnabled(True)
    
    def _create_checkbox_item(self, checked=True):
        item = QTableWidgetItem("")
        item.setFlags(
            QtCore.Qt.ItemFlag.ItemIsUserCheckable |
            QtCore.Qt.ItemFlag.ItemIsEnabled |
            QtCore.Qt.ItemFlag.ItemIsSelectable
        )
        item.setCheckState(
            QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        )
        item.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignCenter))
        return item

    def _on_table_item_changed(self, item):
        if not item or self._checkbox_bulk_change:
            return
        if item.column() != self.COL_INCLUDE:
            return

        row = item.row()
        state = item.checkState()
        shift_pressed = bool(
            QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
        )

        if shift_pressed and self._last_checkbox_row is not None and self._last_checkbox_row != row:
            start_row, end_row = sorted((self._last_checkbox_row, row))
            self._checkbox_bulk_change = True
            try:
                for current_row in range(start_row, end_row + 1):
                    current_item = self.table.item(current_row, self.COL_INCLUDE)
                    if current_item:
                        current_item.setCheckState(state)
            finally:
                self._checkbox_bulk_change = False

        self._last_checkbox_row = row

    def _set_all_chapters_checked(self, checked):
        self._checkbox_bulk_change = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, self.COL_INCLUDE)
                if item:
                    item.setCheckState(
                        QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
                    )
        finally:
            self._checkbox_bulk_change = False

    def _get_build_row_indices(self):
        rows = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue

            include_item = self.table.item(row, self.COL_INCLUDE)
            if include_item and include_item.checkState() == QtCore.Qt.CheckState.Checked:
                rows.append(row)

        return rows

    def _insert_table_row(self, row, file_path, is_custom=False):
        """
        Вставляет строку в таблицу. Используется для добавления кастомных файлов.
        """
        self.table.insertRow(row)
        
        # Колонка 0: Номер (заполнится позже пакетно через _renumber_rows)
        self._checkbox_bulk_change = True
        try:
            self.table.setItem(row, self.COL_INCLUDE, self._create_checkbox_item(True))
        finally:
            self._checkbox_bulk_change = False
        self.table.setItem(row, self.COL_NUMBER, QTableWidgetItem(""))

        # Колонка 1: Описание (Оригинал / Тип)
        # Если это кастомный файл, пишем понятную метку
        display_text = "➕ Доп. файл" if is_custom else (file_path or "???")
        item_desc = QTableWidgetItem(display_text)
        
        # ВАЖНО: Для кастомных файлов UserRole должен быть None.
        # Это сигнал для create_epub использовать имя файла с диска, а не искать его в манифесте.
        internal_data = None if is_custom else file_path
        item_desc.setData(QtCore.Qt.ItemDataRole.UserRole, internal_data)
        item_desc.setFlags(item_desc.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        
        # Если это кастомный файл, можно подсветить его, чтобы отличался
        if is_custom:
            item_desc.setForeground(QtGui.QBrush(QtGui.QColor("#2e7d32"))) # Зеленоватый оттенок текста

        self.table.setItem(row, self.COL_SOURCE, item_desc)

        # Колонка 2: Выбор файла
        # Для добавленного вручную файла сразу создаем комбобокс с этим файлом
        combo = NoScrollComboBox()
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(24)
        file_icon = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon)
        fname = os.path.basename(file_path)
        combo.addItem(file_icon, fname, userData=file_path)
        combo.setCurrentIndex(0)
        combo.currentIndexChanged.connect(self._update_preview_button_state)
        self.table.setCellWidget(row, self.COL_FILE, combo)
        
    def _restore_full_view(self):
        """Режим создания: показываем всё."""
        for i in range(self.table.rowCount()):
            self.table.setRowHidden(i, False)

    def add_custom_chapters(self):
        """Добавление произвольных файлов с выбором места вставки."""
        
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите HTML файлы", self.translated_folder, "HTML files (*.html *.htm *.xhtml)"
        )
        if not files:
            return
        self._structure_modified = True # <--- Флаг
        # 1. Сортируем выбранные файлы "по-умному" (как просил пользователь)
        files.sort(key=extract_number_from_path)

        # 2. Спрашиваем, куда вставить
        items = ["В конец списка", "В начало списка"]
        current_row = self.table.currentRow()
        if current_row >= 0:
            items.append("После выделенной строки")
        
        target, ok = QtWidgets.QInputDialog.getItem(
            self, "Вставка файлов", "Куда добавить новые главы?", items, 0, False
        )
        
        if not ok:
            return

        # Определяем индекс вставки
        insert_at = self.table.rowCount() # Default: End
        if target == "В начало списка":
            insert_at = 0
        elif target == "После выделенной строки" and current_row >= 0:
            insert_at = current_row + 1

        # 3. Вставляем
        for file_path in reversed(files): # Reversed, чтобы при insertAt(index) порядок сохранился
             self._insert_table_row(insert_at, file_path, is_custom=True)
             # Копируем файл в папку проекта, если его там нет
             dest_path = os.path.join(self.translated_folder, os.path.basename(file_path))
             if not os.path.exists(dest_path) or not os.path.samefile(file_path, dest_path):
                 try:
                     shutil.copy2(file_path, dest_path)
                 except:
                     pass # Если не вышло скопировать, используем как есть

        # Обновляем номера строк
        self._renumber_rows()
        
    #: Порог, с которого первичное заполнение таблицы идёт чанками.
    CHUNKED_FILL_THRESHOLD = 150
    #: Строк на один чанк (один тик цикла событий). Комбобоксы с полишем
    #: темы дороги: чанк подобран, чтобы тик укладывался в кадр анимации.
    CHUNKED_FILL_BATCH = 40

    def _smart_update_table(self, target_paths):
        """
        Умная перерисовка с блокировкой сигналов и Diff-алгоритмом.
        """
        # Перезапуск во время незавершённого чанкового заполнения:
        # сбрасываем таблицу и начинаем с чистого листа.
        if self._fill_in_progress:
            self._fill_generation += 1
            self._fill_in_progress = False
            self.table.setRowCount(0)

        # Большое первичное заполнение — чанками, чтобы карточка появлялась
        # мгновенно и анимация не рвалась на 500+ комбобоксах.
        if (
            self.table.rowCount() == 0
            and len(target_paths) > self.CHUNKED_FILL_THRESHOLD
        ):
            self._chunked_fill(list(target_paths))
            return

        # 1. Блокировка всего
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False) # Важно для скорости Qt
        original_selection_mode = self.table.selectionMode()

        try:
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

            # 2. Собираем текущие ID (пути) из таблицы
            current_paths = []
            for row in range(self.table.rowCount()):
                item = self.table.item(row, self.COL_SOURCE)
                # Если item есть и у него есть UserRole - это путь.
                # Если UserRole пустой (кастомный файл) - используем уникальный маркер или имя файла?
                # Для корректного Diff нам нужно отличать строки.
                if item:
                    uid = item.data(QtCore.Qt.ItemDataRole.UserRole)
                    if not uid: 
                        # Это кастомный файл. Используем текст (имя файла) как ID
                        uid = f"__custom__{item.text()}"
                    current_paths.append(uid)
                else:
                    current_paths.append(None) # Битые строки

            # 3. Принимаем решение: Хирургия или Полный сброс
            if current_paths == target_paths:
                # Идеальное совпадение, обновляем только содержимое (статусы, если надо)
                # В данном диалоге контент статичен, поэтому ничего не делаем
                pass
            else:
                self._surgical_update(current_paths, target_paths)
            
            # 4. Всегда обновляем нумерацию строк в конце (это быстро)
            self._renumber_rows()

        finally:
            # 5. Разблокировка
            self.table.setSelectionMode(original_selection_mode)
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

    def _chunked_fill(self, target_paths):
        """Первичное заполнение большой таблицы порциями по тикам цикла.

        Кнопка сборки выключена до завершения: собирать EPUB по
        полузаполненной таблице нельзя.
        """
        self._fill_generation += 1
        generation = self._fill_generation
        self._fill_in_progress = True
        self.create_epub_btn.setEnabled(False)
        self.table.setRowCount(len(target_paths))

        def _fill_from(start: int) -> None:
            if generation != self._fill_generation:
                return  # заполнение перезапущено
            try:
                total = self.table.rowCount()
            except RuntimeError:
                return  # диалог уже уничтожен
            end = min(start + self.CHUNKED_FILL_BATCH, len(target_paths))
            self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for row in range(start, min(end, total)):
                    self._populate_row(row, target_paths[row])
            finally:
                self.table.blockSignals(False)
                self.table.setUpdatesEnabled(True)
            if end < len(target_paths):
                QtCore.QTimer.singleShot(0, lambda: _fill_from(end))
                return
            self._fill_in_progress = False
            self.table.blockSignals(True)
            try:
                self._renumber_rows()
            finally:
                self.table.blockSignals(False)
            self.create_epub_btn.setEnabled(True)
            self._update_preview_button_state()

        _fill_from(0)

    def _surgical_update(self, old_ids, new_ids):
        """
        Реализация Diff алгоритма (Longest Common Subsequence).
        old_ids: список текущих ID в таблице.
        new_ids: целевой список ID.
        """
        n, m = len(old_ids), len(new_ids)

        # Используем твой проверенный алгоритм на Python
        dp = [[0] * (m + 1) for _ in range(n + 1)]
        for i in range(n):
            for j in range(m):
                if old_ids[i] == new_ids[j]:
                    dp[i+1][j+1] = dp[i][j] + 1
                else:
                    dp[i+1][j+1] = max(dp[i+1][j], dp[i][j+1])
        
        i, j = n, m
        ops = []
        while i > 0 or j > 0:
            if i > 0 and j > 0 and old_ids[i-1] == new_ids[j-1]:
                ops.append(('keep', i-1, j-1))
                i -= 1; j -= 1
            elif j > 0 and (i == 0 or dp[i][j-1] >= dp[i-1][j]):
                ops.append(('insert', -1, j-1))
                j -= 1
            elif i > 0 and (j == 0 or dp[i][j-1] < dp[i-1][j]):
                ops.append(('delete', i-1, -1))
                i -= 1
        
        ops.reverse()
        
        # --- Проверка на хаос (Triage) ---
        structural_changes = sum(1 for op in ops if op[0] != 'keep')
        if len(new_ids) > 0 and (structural_changes / len(new_ids) > 0.5) and len(new_ids) > 1000:
            # Если меняется более 50% таблицы при большом размере - быстрее перерисовать всё
            self.table.setRowCount(0)
            self.table.setRowCount(len(new_ids))
            for idx, path in enumerate(new_ids):
                self._populate_row(idx, path)
            return

        # --- Применение операций ---
        current_row = 0
        for op, old_idx, new_idx in ops:
            if op == 'delete':
                self.table.removeRow(current_row)
                # current_row НЕ увеличиваем, т.к. следующая строка сдвинулась на место удаленной
            elif op == 'insert':
                target_path = new_ids[new_idx]
                self.table.insertRow(current_row)
                self._populate_row(current_row, target_path)
                current_row += 1
            elif op == 'keep':
                # Строка совпадает.
                # Можно вызвать _populate_row(..., update_only=True), если нужно обновить данные внутри.
                # В данном случае, если ID (путь) совпал, то контент (QComboBox) скорее всего верный.
                current_row += 1

    def _populate_row(self, row, internal_path, update_only=False):
        """
        Создает или обновляет строку таблицы.
        """
        # Колонка 0: Номер (обновим пакетно в конце через _renumber_rows)
        if not update_only:
            self._checkbox_bulk_change = True
            try:
                self.table.setItem(row, self.COL_INCLUDE, self._create_checkbox_item(True))
            finally:
                self._checkbox_bulk_change = False
            self.table.setItem(row, self.COL_NUMBER, QTableWidgetItem(""))

        # Колонка 1: Путь (Оригинал)
        display_text = internal_path if internal_path else "???"
        item_path = self.table.item(row, self.COL_SOURCE)
        if not item_path:
            item_path = QTableWidgetItem(display_text)
            self.table.setItem(row, self.COL_SOURCE, item_path)
        else:
            item_path.setText(display_text)
        
        item_path.setToolTip(f"Оригинальный путь в EPUB:\n{internal_path}")
        item_path.setData(QtCore.Qt.ItemDataRole.UserRole, internal_path)
        item_path.setFlags(item_path.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)

        combo = NoScrollComboBox()
        combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(24)
        file_icon = self._version_file_icon
        validated_icon = self._version_validated_icon

        versions = self.project_manager.get_versions_for_original(internal_path)
        sorted_versions = sort_translation_versions_for_epub_build(versions, self.translated_folder)

        for version_info in sorted_versions:
            icon = validated_icon if version_info['suffix'] == '_validated.html' else file_icon
            display_text = os.path.basename(version_info['filepath'])
            combo.addItem(icon, display_text, userData=version_info['filepath'])

        combo.setCurrentIndex(0 if combo.count() else -1)
        combo.currentIndexChanged.connect(self._update_preview_button_state)
        self.table.setCellWidget(row, self.COL_FILE, combo)
            
    def _renumber_rows(self):
        for i in range(self.table.rowCount()):
            self.table.setItem(i, self.COL_NUMBER, QTableWidgetItem(str(i + 1)))

    def _move_row(self, offset):
        """Перемещение выделенной строки на offset."""
        row = self.table.currentRow()
        if row < 0: return
        self._structure_modified = True # <--- Флаг
        
        new_row = row + offset
        if 0 <= new_row < self.table.rowCount():
            self._swap_rows(row, new_row)
            self.table.selectRow(new_row)

    def _move_row_extreme(self, top=True):
        """Перемещение в самый верх или низ."""
        row = self.table.currentRow()
        if row < 0: return
        
        self._structure_modified = True # <--- Флаг
        
        target_row = 0 if top else self.table.rowCount() - 1
        
        # Мы удаляем строку и вставляем её в новое место
        # Но QTableWidget не поддерживает простое перемещение виджетов (ComboBox).
        # Проще менять местами соседние строки в цикле, пока не дойдем до края.
        
        direction = -1 if top else 1
        current = row
        while current != target_row:
            next_r = current + direction
            self._swap_rows(current, next_r)
            current = next_r
        
        self.table.selectRow(target_row)

    def _swap_rows(self, row1, row2):
        """Обмен содержимым двух строк, включая виджеты."""
        # 1. Забираем виджеты
        w1 = self.table.cellWidget(row1, self.COL_FILE)
        w2 = self.table.cellWidget(row2, self.COL_FILE)
        
        # 2. Клонируем данные элементов (текст, UserData)
        for col in [self.COL_INCLUDE, self.COL_NUMBER, self.COL_SOURCE]:
            item1 = self.table.takeItem(row1, col)
            item2 = self.table.takeItem(row2, col)
            self.table.setItem(row2, col, item1)
            self.table.setItem(row1, col, item2)
        
        # 3. Возвращаем виджеты на новые места
        # Внимание: setCellWidget переносит владение. 
        # Нам нужно пересоздать их или перепривязать.
        # Просто перенос может не сработать, если Qt удалит виджет при takeItem/setItem.
        # Поэтому надежнее создать новые виджеты с теми же данными.
        
        def clone_combo(old_combo):
            new_combo = NoScrollComboBox()
            new_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            new_combo.setMinimumContentsLength(24)
            if old_combo:
                for i in range(old_combo.count()):
                    new_combo.addItem(old_combo.itemIcon(i), old_combo.itemText(i), userData=old_combo.itemData(i))
                new_combo.setCurrentIndex(old_combo.currentIndex())
            new_combo.currentIndexChanged.connect(self._update_preview_button_state)
            return new_combo

        self.table.setCellWidget(row1, self.COL_FILE, clone_combo(w2))
        self.table.setCellWidget(row2, self.COL_FILE, clone_combo(w1))
        
        self._renumber_rows()
    
    def _select_original_epub(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Выберите оригинальный EPUB файл", "", "EPUB files (*.epub)")
        if filepath:
            self.original_epub_path = filepath
            self.original_file_label.setText(os.path.basename(filepath))

    def load_chapters(self):
        """
        Точка входа. Собирает целевой список глав и запускает умное обновление.
        """
        try:
            # 1. Собираем список того, что ДОЛЖНО быть (Target)
            all_internal_paths = self.project_manager.get_all_originals()
            if not all_internal_paths: 
                self.table.setRowCount(0)
                self._update_preview_button_state()
                return

            # Логика сортировки (как и раньше)
            canonical_order, sort_method = get_epub_chapter_order(self.original_epub_path, return_method=True)
            target_paths = []

            # Если включен режим обновления оригинала - показываем только оригинальные
            # Если "Создать новый" - показываем всё, что есть в проекте (включая кастомные, если они зарегистрированы в project_manager)
            # В текущей логике project_manager хранит оригиналы. Кастомные файлы без оригиналов
            # обрабатываются отдельно, если они добавлены в self.table. 
            # НО: load_chapters вызывается чтобы "сбросить" состояние к правильному (например, при смене режима).
            
            # ВАЖНО: При смене режима на "Обновление", мы хотим видеть только канон.
            # При смене на "Новый", мы хотим видеть канон + возможно то, что уже было добавлено вручную.
            # Но load_chapters по своей сути загружает "базу".
            # Если мы хотим сохранить ручные добавления при перезагрузках, их надо где-то хранить.
            # Пока считаем, что load_chapters приводит таблицу к "чистому" состоянию из ProjectManager.

            if sort_method == 'spine' and canonical_order:
                order_map = {path: i for i, path in enumerate(canonical_order)}
                in_canon = [p for p in all_internal_paths if p in order_map]
                orphans = [p for p in all_internal_paths if p not in order_map]
                in_canon.sort(key=lambda p: order_map[p])
                orphans.sort(key=extract_number_from_path)
                target_paths = in_canon + orphans
                if self.sort_by_last_number_checkbox: self.sort_by_last_number_checkbox.setVisible(False)
            else:
                show_alt_sort_option = any(len(re.findall(r"\d+", os.path.basename(path))) >= 2 for path in all_internal_paths)
                if self.sort_by_last_number_checkbox: self.sort_by_last_number_checkbox.setVisible(show_alt_sort_option)
                sort_key_func = extract_number_from_path_reversed if self.sort_by_last_number_checkbox and self.sort_by_last_number_checkbox.isChecked() else extract_number_from_path
                target_paths = sorted(all_internal_paths, key=sort_key_func)

            # Если мы в режиме обновления - фильтруем строго по наличию в оригиналах (что уже сделано выше)
            # Если в режиме создания - теоретически тут могли быть кастомные, но project_manager.get_all_originals() вернет только оригиналы.
            # Кастомные файлы добавляются вручную через add_custom_chapters.
            # Чтобы они не пропадали при сортировке, нам нужно их подмешать, если они уже есть в таблице?
            # В данном кейсе (удаление файла -> переход в Update Mode), target_paths это "чистый" список.
            # Алгоритм Диффа сам увидит, что кастомные файлы (которые есть в таблице, но нет в target) нужно удалить.
            
            self._smart_update_table(target_paths)
            self._structure_modified = False
            self._update_preview_button_state()

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка загрузки", f"Ошибка: {e}\n{traceback.format_exc()}")
    
   
    def replace_selected_file(self):
        current_row = self.table.currentRow()
        if current_row < 0: return
        # --- редлагаем выбрать из папки проекта ---
        filepath, _ = QFileDialog.getOpenFileName(self, "Выберите HTML файл для замены", self.translated_folder, "HTML files (*.html *.htm)")
        if filepath:
            shutil.copy2(filepath, self.chapters_data[current_row]['filepath'])
            self.load_chapters()
            self.table.selectRow(current_row)

    def delete_selected_file(self):
        """
        Удаление с выбором:
        1. Удалить физически с диска.
        2. Просто убрать строку из таблицы (исключить из сборки).
        """
        current_row = self.table.currentRow()
        if current_row < 0:
            return

        # 1. Сбор данных о жертве
        internal_path_item = self.table.item(current_row, self.COL_SOURCE)
        version_combo = self.table.cellWidget(current_row, self.COL_FILE)
        
        filepath_to_handle = None
        if version_combo:
            filepath_to_handle = version_combo.currentData()
            
        filename_display = os.path.basename(filepath_to_handle) if filepath_to_handle else "выбранный элемент"

        # 2. Создаем диалог с тремя путями
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Варианты удаления")
        msg_box.setText(f"Что сделать с элементом:\n<b>{filename_display}</b>?")
        msg_box.setIcon(QMessageBox.Icon.Question)
        
        # Настраиваем кнопки
        # Role Destructive -> Красная кнопка (обычно) или слева
        btn_delete_disk = msg_box.addButton("🗑 Удалить файл с диска", QMessageBox.ButtonRole.DestructiveRole)
        # Role Action -> Обычная кнопка действия
        btn_remove_list = msg_box.addButton("❌ Просто убрать из списка", QMessageBox.ButtonRole.ActionRole)
        # Role Reject -> Escape / Отмена
        btn_cancel = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        
        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked == btn_cancel:
            return

        # 3. Ветка физического уничтожения
        if clicked == btn_delete_disk:
            try:
                if filepath_to_handle and os.path.exists(filepath_to_handle):
                    os.remove(filepath_to_handle)

                    # Если файл был привязан к структуре проекта, обновляем JSON проекта
                    if internal_path_item:
                        internal_path = internal_path_item.data(QtCore.Qt.ItemDataRole.UserRole)
                        # internal_path может быть пустым, если это кастомный файл.
                        # Если не пустой - значит это перевод оригинальной главы.
                        if internal_path: 
                            from ...api import config as api_config
                            suffix_to_remove = None
                            # Пытаемся понять, какой именно суффикс был у файла
                            for suffix in api_config.all_translated_suffixes() + ["_validated.html"]:
                                if filepath_to_handle.endswith(suffix):
                                    suffix_to_remove = suffix
                                    break
                            
                            if suffix_to_remove:
                                self.project_manager.remove_translation(internal_path, suffix_to_remove)
            except Exception as e:
                QMessageBox.critical(self, "Ошибка ввода-вывода", f"Не удалось удалить файл с диска:\n{e}")
                return

        # 4. Ветка визуального удаления (общая для обоих случаев)
        # Мы просто удаляем строку UI. Это мгновенная операция (0.00ms).
        self.table.removeRow(current_row)
        
        # Быстрый пересчет номеров (текстовая операция)
        self._renumber_rows()
        
        # Ставим флаг, что структура изменена. 
        # (Чтобы при смене режима "Обновление/Создание" сработала полная проверка Diff)
        self._structure_modified = True


    def _get_selected_chapter_filepath(self):
        row = self.table.currentRow()
        if row < 0 or self.table.isRowHidden(row):
            return None

        version_combo = self.table.cellWidget(row, self.COL_FILE)
        if not version_combo:
            return None

        filepath = version_combo.currentData()
        if isinstance(filepath, str) and filepath.strip():
            return filepath
        return None

    def _get_selected_original_internal_path(self):
        row = self.table.currentRow()
        if row < 0 or self.table.isRowHidden(row):
            return None

        item = self.table.item(row, self.COL_SOURCE)
        if not item:
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _select_chapter_by_filepath(self, filepath):
        if not filepath:
            return

        for row in range(self.table.rowCount()):
            version_combo = self.table.cellWidget(row, self.COL_FILE)
            if not version_combo:
                continue
            combo_index = version_combo.findData(filepath)
            if combo_index >= 0:
                self.table.selectRow(row)
                version_combo.setCurrentIndex(combo_index)
                return

    def _get_build_chapter_filepaths(self):
        filepaths = []
        for row in self._get_build_row_indices():
            version_combo = self.table.cellWidget(row, self.COL_FILE)
            if not version_combo:
                continue
            filepath = version_combo.currentData()
            if isinstance(filepath, str) and filepath.strip() and os.path.exists(filepath):
                filepaths.append(filepath)
        return filepaths

    def _open_duplicate_cleanup_for_translated_files(self):
        if not BS4_AVAILABLE:
            QMessageBox.warning(
                self,
                "Недоступно",
                "Для поиска повторов нужен пакет beautifulsoup4. Сейчас он не найден."
            )
            return

        chapter_filepaths = self._get_build_chapter_filepaths()
        if not chapter_filepaths:
            QMessageBox.information(
                self,
                "Нет файлов",
                "Не найдено отмеченных глав с существующими файлами для анализа."
            )
            return

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Поиск повторов")
        self.wait_dialog.setText(
            "Анализируются итоговые главы проекта...\n"
            "Ищем повторы в начале главы и на стыках/в концовках между главами."
        )
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.duplicate_file_analysis_thread = HtmlDuplicateAnalysisThread(chapter_filepaths, self)
        self.duplicate_file_analysis_thread.analysis_finished.connect(self._on_translated_duplicate_analysis_finished)
        self.duplicate_file_analysis_thread.start()
        self.wait_dialog.show()

    def _on_translated_duplicate_analysis_finished(self, analysis_data):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        dialog = EpubDuplicateReviewDialog(analysis_data or {}, self)
        if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
            selected_findings = dialog.get_selected_findings()
            if selected_findings:
                self._run_duplicate_cleanup_for_translated_files(selected_findings)

    def _run_duplicate_cleanup_for_translated_files(self, selected_findings):
        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Удаление повторов")
        self.wait_dialog.setText(
            "Удаляются выбранные повторы из итоговых глав...\n"
            "Изменения записываются прямо в html/xhtml файлы проекта."
        )
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)

        self.duplicate_file_cleanup_thread = HtmlDuplicateCleanupThread(selected_findings, self)
        self.duplicate_file_cleanup_thread.finished_cleanup.connect(self._on_translated_duplicate_cleanup_finished)
        self.duplicate_file_cleanup_thread.start()
        self.wait_dialog.show()

    def _on_translated_duplicate_cleanup_finished(self, success, message):
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()

        if not success:
            QMessageBox.critical(self, "Ошибка удаления", message)
            return

        self.project_manager.reload_data_from_disk()
        self.load_chapters()
        self._update_preview_button_state()
        chapter_filepaths = self._get_build_chapter_filepaths()
        followup_data = {}
        if chapter_filepaths:
            followup_analysis = HtmlDuplicateAnalysisThread(chapter_filepaths, self)
            followup_analysis.analysis_finished.connect(lambda data: followup_data.setdefault('data', data))
            followup_analysis.run()

        remaining = followup_data.get('data') or {}
        remaining_count = len(remaining.get('start_findings') or []) + len(remaining.get('boundary_findings') or [])
        if remaining_count > 0:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Есть ещё повторы")
            msg_box.setText(
                f"{message}\n\nПосле пересчёта найдено ещё {remaining_count} последовательных серий дублей."
            )
            msg_box.setInformativeText("Открыть инструмент повторно и продолжить удаление?")
            yes_button = msg_box.addButton("Да, продолжить", QMessageBox.ButtonRole.YesRole)
            msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg_box.exec()

            if msg_box.clickedButton() == yes_button:
                dialog = EpubDuplicateReviewDialog(remaining, self)
                if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
                    selected_findings = dialog.get_selected_findings()
                    if selected_findings:
                        self._run_duplicate_cleanup_for_translated_files(selected_findings)
                        return

        QMessageBox.information(self, "Повторы удалены", message)

    def _update_preview_button_state(self):
        if not hasattr(self, 'btn_preview'):
            return

        filepath = self._get_selected_chapter_filepath()
        is_ready = bool(filepath and os.path.exists(filepath))
        self.btn_preview.setEnabled(is_ready)
        self.btn_preview.setToolTip(
            filepath if is_ready else "Выберите строку с существующим файлом перевода."
        )

    def _preview_selected_chapter(self, *_):
        filepath = self._get_selected_chapter_filepath()
        if not filepath:
            QMessageBox.information(self, "Предпросмотр", "Сначала выберите главу в таблице.")
            return

        if not os.path.exists(filepath):
            QMessageBox.warning(
                self,
                "Файл не найден",
                f"Не удалось открыть файл предпросмотра:\n{filepath}",
            )
            self._update_preview_button_state()
            return

        self._open_chapter_preview(filepath)

    def _open_chapter_preview(self, filepath):
        current_row = self.table.currentRow()
        dialog = ChapterEditorDialog(
            filepath,
            parent=self,
            original_epub_path=self.original_epub_path,
            original_internal_path=self._get_selected_original_internal_path(),
            project_manager=self.project_manager,
        )
        exec_dialog(self, dialog)

        self.load_chapters()
        self._select_chapter_by_filepath(filepath)
        if current_row >= 0 and self.table.currentRow() < 0 and current_row < self.table.rowCount():
            self.table.selectRow(current_row)
        self._update_preview_button_state()

    def _update_cover_ui(self):
        is_custom = self.cover_mode_custom.isChecked()
        self.btn_select_cover.setEnabled(is_custom)
        
        if not is_custom:
            if self.original_epub_path:
                self.lbl_cover_status.setText(f"(Из {os.path.basename(self.original_epub_path)})")
            else:
                self.lbl_cover_status.setText("(Требуется оригинал)")
        else:
            if self.custom_cover_path:
                self.lbl_cover_status.setText(os.path.basename(self.custom_cover_path))
            else:
                self.lbl_cover_status.setText("<не выбрана>")

    def _select_custom_cover(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Выберите изображение обложки", self.translated_folder, "Images (*.jpg *.jpeg *.png)"
        )
        if filepath:
            self.custom_cover_path = filepath
            self._update_cover_ui()

    def create_epub(self):
        build_rows = self._get_build_row_indices()
        if not build_rows:
            QtWidgets.QMessageBox.warning(
                self,
                "Нет выбранных глав",
                "Отметьте хотя бы одну главу для сборки EPUB."
            )
            return

        # 1. Проверка на недостающие файлы (как было)
        missing_files = []
        for i in build_rows:
            # Пропускаем скрытые строки (например, в режиме Update скрыты кастомные)
            version_combo = self.table.cellWidget(i, self.COL_FILE)
            if version_combo:
                selected_filepath = version_combo.currentData()
                if not os.path.exists(selected_filepath):
                    missing_files.append(os.path.basename(selected_filepath))
        
        if missing_files:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Отсутствуют файлы для сборки")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            
            # Формируем информативное сообщение
            details = "\n".join([f"- {f}" for f in missing_files[:5]])
            if len(missing_files) > 5:
                details += f"\n… и еще {len(missing_files) - 5}."
            
            msg_box.setText(f"<b>Обнаружено {len(missing_files)} отсутствующих на диске файлов, которые выбраны для сборки.</b>")
            msg_box.setInformativeText(f"Это могло произойти, если файлы были удалены вручную.\n\nПримеры:\n{details}\n\nРекомендуется запустить сверку проекта, чтобы обновить список доступных файлов. Запустить сейчас?")
            
            # Добавляем кнопки
            yes_button = msg_box.addButton("Да, запустить сверку", QMessageBox.ButtonRole.YesRole)
            no_button = msg_box.addButton("Нет, отменить сборку", QMessageBox.ButtonRole.NoRole)
            
            msg_box.exec()
            
            if msg_box.clickedButton() == yes_button:
                # Запускаем фоновую сверку и перезагрузку
                self._run_project_sync_and_reload()
            
            # В любом случае прерываем текущую операцию сборки
            return
            
        # --- КОНЕЦ НОВОГО КОДА ---

        if self.create_new_radio.isChecked():
            self._create_new_epub_with_creator(build_rows)
        elif self.update_original_radio.isChecked():
            self._update_original_epub_with_updater(build_rows)

    def _create_new_epub_with_creator(self, build_rows):
        title = self.title_edit.text() or "Переведенная книга"
        author = self.author_edit.text() or "Неизвестный автор"
        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        output_path, _ = QFileDialog.getSaveFileName(self, "Сохранить EPUB", f"{safe_title}.epub", "EPUB files (*.epub)")
        if not output_path: return

        try:
            creator = EpubCreator(title, author)
            
            # --- ЛОГИКА ОБЛОЖКИ (из предыдущего шага) ---
            cover_file_path = None
            
            if self.cover_mode_custom.isChecked() and self.custom_cover_path:
                cover_file_path = self.custom_cover_path
            
            elif self.cover_mode_original.isChecked() and self.original_epub_path:
                try:
                    with zipfile.ZipFile(self.original_epub_path, 'r') as zf:
                        cover_item = None
                        try:
                             opf_content = zf.read('OEBPS/content.opf')
                        except:
                             opf_path = [f for f in zf.namelist() if f.endswith('.opf')][0]
                             opf_content = zf.read(opf_path)
                        
                        if opf_content:
                            root = SafeET.fromstring(opf_content)
                            ns = {'opf': 'http://www.idpf.org/2007/opf'}
                            meta_cover = root.find('.//opf:meta[@name="cover"]', ns)
                            if meta_cover:
                                cover_id = meta_cover.get('content')
                                item = root.find(f'.//opf:item[@id="{cover_id}"]', ns)
                                if item:
                                    cover_href = item.get('href')
                                    for name in zf.namelist():
                                        if name.endswith(cover_href):
                                            cover_item = name
                                            break
                        
                        if not cover_item:
                            for name in zf.namelist():
                                if 'cover' in name.lower() and name.lower().endswith(('.jpg', '.jpeg', '.png')):
                                    cover_item = name
                                    break
                        
                        if cover_item:
                            temp_cover = os.path.join(self.translated_folder, "temp_extracted_cover" + os.path.splitext(cover_item)[1])
                            with open(temp_cover, 'wb') as f:
                                f.write(zf.read(cover_item))
                            cover_file_path = temp_cover
                            
                except Exception as e:
                    print(f"Ошибка извлечения обложки: {e}")

            if cover_file_path:
                creator.set_cover(cover_file_path)
            # ----------------------------------------------
            
            for build_index, i in enumerate(build_rows, start=1):
                internal_path_item = self.table.item(i, self.COL_SOURCE)
                version_combo = self.table.cellWidget(i, self.COL_FILE)
                
                if not internal_path_item or not version_combo: continue

                selected_filepath = version_combo.currentData()
                original_internal_path = internal_path_item.data(QtCore.Qt.ItemDataRole.UserRole)
                
                # Читаем контент
                with open(selected_filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                # Получаем заголовок и ОБНОВЛЯЕМ <title>
                chapter_title = f"Глава {build_index}"
                if BS4_AVAILABLE:
                    soup = BeautifulSoup(content, 'html.parser')
                    h_tags = soup.find(['h1', 'h2', 'h3'])
                    
                    if h_tags:
                        chapter_title = h_tags.get_text().strip()
                        
                        # --- НОВАЯ ЛОГИКА: Обновляем <title> внутри HTML ---
                        if soup.title:
                            soup.title.string = chapter_title
                        elif soup.head:
                            new_title_tag = soup.new_tag("title")
                            new_title_tag.string = chapter_title
                            soup.head.append(new_title_tag)
                        
                        # Сериализуем обратно в строку с обновленным title
                        content = str(soup)
                        # ---------------------------------------------------
                
                if original_internal_path:
                    filename_in_epub = os.path.basename(original_internal_path)
                else:
                    filename_in_epub = os.path.basename(selected_filepath)
                
                creator.add_chapter(filename_in_epub, content, chapter_title)
            
            creator.create_epub(output_path)
            
            # Чистим временную обложку
            if cover_file_path and "temp_extracted_cover" in cover_file_path and os.path.exists(cover_file_path):
                try: os.remove(cover_file_path)
                except: pass
                
            QtWidgets.QMessageBox.information(self, "Успех", f"EPUB успешно создан: {output_path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось создать EPUB: {e}\n{traceback.format_exc()}")      

    def _update_original_epub_with_updater(self, build_rows):
        if not self.original_epub_path:
            QtWidgets.QMessageBox.warning(self, "Файл не выбран", "Пожалуйста, укажите путь к оригинальному EPUB файлу.")
            return

        safe_title = os.path.splitext(os.path.basename(self.original_epub_path))[0]
        output_path, _ = QFileDialog.getSaveFileName(self, "Сохранить обновленный EPUB", f"{safe_title}_updated.epub", "EPUB files (*.epub)")
        if not output_path: return

        try:
            from ...utils.epub_tools import EpubUpdater # Локальный импорт
            updater = EpubUpdater(self.original_epub_path)
            
            # Собираем данные из таблицы
            for i in build_rows:
                internal_path_item = self.table.item(i, self.COL_SOURCE)
                version_combo = self.table.cellWidget(i, self.COL_FILE)
                
                if not internal_path_item or not version_combo:
                    continue

                selected_filepath = version_combo.currentData()
                internal_path_to_replace = internal_path_item.data(QtCore.Qt.ItemDataRole.UserRole)
                
                # Вместо контента файла, передаем путь к нему
                updater.add_replacement(internal_path_to_replace, selected_filepath)
            
            updater.update_and_save(output_path)
            QtWidgets.QMessageBox.information(self, "Успех", f"EPUB успешно обновлен и сохранен: {output_path}")

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка обновления", f"Не удалось обновить EPUB: {e}\n{traceback.format_exc()}")
            
            
            
# В классе TranslatedChaptersManagerDialog

    def _run_project_sync_and_reload(self):
        """Запускает синхронизацию проекта в фоновом потоке и перезагружает таблицу по завершении."""
        if not self.project_manager or not self.original_epub_path:
            QMessageBox.warning(self, "Ошибка", "Невозможно запустить сверку: не определен проект или путь к исходному EPUB.")
            return

        self.wait_dialog = QMessageBox(self)
        self.wait_dialog.setWindowTitle("Синхронизация")
        self.wait_dialog.setText("Идет анализ и сверка проекта…")
        self.wait_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton)
        self.wait_dialog.setModal(True)
        
        migrator = ProjectMigrator(self.translated_folder, self.original_epub_path, self.project_manager)
        
        self.sync_thread = SyncThread(migrator, parent_widget=self)
        self.sync_thread.finished_sync.connect(self._on_sync_finished)
        
        self.sync_thread.start()
        self.wait_dialog.show()

    def _on_sync_finished(self, is_project_ready, message):
        """Слот, который вызывается после завершения фоновой синхронизации."""
        if hasattr(self, 'wait_dialog') and self.wait_dialog:
            self.wait_dialog.accept()
    
        if not is_project_ready:
            QMessageBox.warning(self, "Операция прервана", message)
            return
            
        # Перезагружаем данные из файла и обновляем таблицу в UI
        self.project_manager.reload_data_from_disk()
        self.load_chapters()
        QMessageBox.information(self, "Синхронизация завершена", f"{message}\n\nСписок файлов в сборщике обновлен.")
        
        
class EpubDuplicateAnalysisThread(QThread):
    analysis_finished = pyqtSignal(object)

    def __init__(self, virtual_epub_path, chapters_list, parent=None):
        super().__init__(parent)
        self.virtual_epub_path = virtual_epub_path
        self.chapters_list = chapters_list or []

    def run(self):
        if not BS4_AVAILABLE:
            self.analysis_finished.emit({'start_findings': [], 'boundary_findings': []})
            return

        try:
            chapter_infos = []
            with open(self.virtual_epub_path, "rb") as epub_file, \
                    zipfile.ZipFile(epub_file, "r") as zf:
                for chapter_index, chapter_path in enumerate(self.chapters_list):
                    try:
                        content = zf.read(chapter_path).decode('utf-8', errors='ignore')
                    except Exception:
                        continue

                    soup = BeautifulSoup(content, 'html.parser')
                    blocks = extract_duplicate_review_blocks(soup)
                    if not blocks:
                        continue

                    chapter_infos.append({
                        'index': chapter_index,
                        'path': chapter_path,
                        'name': os.path.basename(chapter_path),
                        'blocks': blocks,
                    })

            self.analysis_finished.emit(analyze_duplicate_findings(chapter_infos))
        except Exception:
            print(traceback.format_exc())
            self.analysis_finished.emit({'start_findings': [], 'boundary_findings': []})


class EpubDuplicateCleanupThread(QThread):
    finished_cleanup = pyqtSignal(object, str)

    def __init__(self, virtual_epub_path, findings, parent=None):
        super().__init__(parent)
        self.virtual_epub_path = virtual_epub_path
        self.findings = findings or []

    def run(self):
        if not BS4_AVAILABLE:
            self.finished_cleanup.emit(None, "Для удаления повторов нужен пакет beautifulsoup4.")
            return

        try:
            grouped_findings = {}
            for finding in self.findings:
                chapter_path = finding.get('chapter_path')
                tag_paths = finding.get('tag_paths') or []
                if not tag_paths and finding.get('tag_path'):
                    tag_paths = [finding.get('tag_path')]
                if not chapter_path or not tag_paths:
                    continue
                grouped_findings.setdefault(chapter_path, {})
                for tag_path in tag_paths:
                    normalized_path = tuple(tag_path or [])
                    if not normalized_path:
                        continue
                    grouped_findings[chapter_path][normalized_path] = finding

            total_removed = 0
            touched_chapters = 0
            temp_output_buffer = io.BytesIO()

            with open(self.virtual_epub_path, 'rb') as epub_file, \
                    zipfile.ZipFile(epub_file, 'r') as zin:
                with zipfile.ZipFile(temp_output_buffer, 'w', zipfile.ZIP_DEFLATED) as zout:
                    all_files_content = {}
                    for item in zin.infolist():
                        all_files_content[item.filename] = zin.read(item.filename)

                    for chapter_path, finding_map in grouped_findings.items():
                        content_bytes = all_files_content.get(chapter_path)
                        if content_bytes is None:
                            continue

                        content_str = content_bytes.decode('utf-8', errors='ignore')
                        had_xml_declaration = content_str.lstrip().startswith('<?xml')
                        soup = BeautifulSoup(content_str, 'html.parser')
                        root = soup.body or soup
                        removed_in_chapter = 0

                        for tag_path in sorted(finding_map.keys(), reverse=True):
                            target_tag = resolve_tag_path(root, list(tag_path))
                            if target_tag is None or not getattr(target_tag, 'name', None):
                                continue
                            if str(target_tag.name).lower() == 'h1':
                                continue
                            target_tag.decompose()
                            removed_in_chapter += 1

                        if removed_in_chapter:
                            updated_content = str(soup)
                            if had_xml_declaration and not updated_content.lstrip().startswith('<?xml'):
                                updated_content = '<?xml version="1.0" encoding="utf-8"?>\n' + updated_content
                            all_files_content[chapter_path] = updated_content.encode('utf-8')
                            total_removed += removed_in_chapter
                            touched_chapters += 1

                    for filename, content in all_files_content.items():
                        zout.writestr(filename, content)

            temp_output_buffer.seek(0)
            with open(self.virtual_epub_path, 'wb') as fh:
                fh.write(temp_output_buffer.getvalue())

            self.finished_cleanup.emit(
                self.virtual_epub_path,
                (
                    "Удаление повторов завершено.\n"
                    f"Удалено блоков: {total_removed}.\n"
                    f"Изменено глав: {touched_chapters}."
                ),
            )
        except Exception as e:
            print(traceback.format_exc())
            self.finished_cleanup.emit(None, f"Ошибка удаления повторов: {e}")


class HtmlDuplicateAnalysisThread(QThread):
    analysis_finished = pyqtSignal(object)

    def __init__(self, chapter_filepaths, parent=None):
        super().__init__(parent)
        self.chapter_filepaths = chapter_filepaths or []

    def run(self):
        if not BS4_AVAILABLE:
            self.analysis_finished.emit({'start_findings': [], 'boundary_findings': []})
            return

        try:
            chapter_infos = []
            for chapter_index, chapter_path in enumerate(self.chapter_filepaths):
                if not chapter_path or not os.path.exists(chapter_path):
                    continue

                try:
                    with open(chapter_path, 'r', encoding='utf-8', errors='ignore') as fh:
                        content = fh.read()
                except Exception:
                    continue

                soup = BeautifulSoup(content, 'html.parser')
                blocks = extract_duplicate_review_blocks(soup)
                if not blocks:
                    continue

                chapter_infos.append({
                    'index': chapter_index,
                    'path': chapter_path,
                    'name': os.path.basename(chapter_path),
                    'blocks': blocks,
                })

            self.analysis_finished.emit(analyze_duplicate_findings(chapter_infos))
        except Exception:
            print(traceback.format_exc())
            self.analysis_finished.emit({'start_findings': [], 'boundary_findings': []})


class HtmlDuplicateCleanupThread(QThread):
    finished_cleanup = pyqtSignal(bool, str)

    def __init__(self, findings, parent=None):
        super().__init__(parent)
        self.findings = findings or []

    def run(self):
        if not BS4_AVAILABLE:
            self.finished_cleanup.emit(False, "Для удаления повторов нужен пакет beautifulsoup4.")
            return

        try:
            grouped_findings = {}
            for finding in self.findings:
                chapter_path = finding.get('chapter_path')
                tag_paths = finding.get('tag_paths') or []
                if not tag_paths and finding.get('tag_path'):
                    tag_paths = [finding.get('tag_path')]
                if not chapter_path or not tag_paths:
                    continue
                grouped_findings.setdefault(chapter_path, {})
                for tag_path in tag_paths:
                    normalized_path = tuple(tag_path or [])
                    if not normalized_path:
                        continue
                    grouped_findings[chapter_path][normalized_path] = finding

            total_removed = 0
            touched_files = 0

            for chapter_path, finding_map in grouped_findings.items():
                if not os.path.exists(chapter_path):
                    continue

                with open(chapter_path, 'r', encoding='utf-8', errors='ignore') as fh:
                    content = fh.read()

                had_xml_declaration = content.lstrip().startswith('<?xml')
                soup = BeautifulSoup(content, 'html.parser')
                root = soup.body or soup
                removed_in_file = 0

                for tag_path in sorted(finding_map.keys(), reverse=True):
                    target_tag = resolve_tag_path(root, list(tag_path))
                    if target_tag is None or not getattr(target_tag, 'name', None):
                        continue
                    if str(target_tag.name).lower() == 'h1':
                        continue
                    target_tag.decompose()
                    removed_in_file += 1

                if removed_in_file:
                    updated_content = str(soup)
                    if had_xml_declaration and not updated_content.lstrip().startswith('<?xml'):
                        updated_content = '<?xml version="1.0" encoding="utf-8"?>\n' + updated_content
                    with open(chapter_path, 'w', encoding='utf-8', errors='ignore') as fh:
                        fh.write(updated_content)
                    total_removed += removed_in_file
                    touched_files += 1

            self.finished_cleanup.emit(
                True,
                (
                    "Удаление повторов завершено.\n"
                    f"Удалено блоков: {total_removed}.\n"
                    f"Изменено файлов: {touched_files}."
                ),
            )
        except Exception as e:
            print(traceback.format_exc())
            self.finished_cleanup.emit(False, f"Ошибка удаления повторов: {e}")


class EpubDuplicateReviewDialog(QDialog):
    def __init__(self, analysis_data, parent=None):
        super().__init__(parent)
        self.analysis_data = analysis_data or {}
        self.selected_findings = []
        self.setWindowTitle("Повторы в главах EPUB")
        self.resize(980, 680)
        self._tab_tables = {}
        self._tab_previews = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Один инструмент с двумя функциями: повторы в начале главы и повторы на стыках/в концовках между главами.\n"
            "Показываются только последовательные серии дублей с начала или с конца главы. Заголовки <h1> не удаляются."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tabs = QTabWidget()
        install_tab_fade(self.tabs)
        self._build_results_tab(
            tab_key='start',
            tab_title="Начало главы",
            findings=self.analysis_data.get('start_findings') or [],
            empty_text="Повторы в первых строках глав не найдены.",
        )
        self._build_results_tab(
            tab_key='boundary',
            tab_title="Стыки и концовки",
            findings=self.analysis_data.get('boundary_findings') or [],
            empty_text="Повторы на стыках и в концовках глав не найдены.",
        )
        layout.addWidget(self.tabs, 1)

        actions_layout = QHBoxLayout()
        self.btn_check_current = QPushButton("Отметить всё на вкладке")
        self.btn_uncheck_current = QPushButton("Снять всё на вкладке")
        self.btn_check_current.clicked.connect(lambda: self._set_current_tab_checked(True))
        self.btn_uncheck_current.clicked.connect(lambda: self._set_current_tab_checked(False))
        actions_layout.addWidget(self.btn_check_current)
        actions_layout.addWidget(self.btn_uncheck_current)
        actions_layout.addStretch()
        layout.addLayout(actions_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Удалить отмеченное")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Закрыть")
        buttons.accepted.connect(self._collect_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_results_tab(self, tab_key, tab_title, findings, empty_text):
        container = QWidget()
        tab_layout = QVBoxLayout(container)

        summary = QLabel(f"Найдено: {len(findings)}")
        summary.setStyleSheet("font-weight: bold;")
        tab_layout.addWidget(summary)

        table = QTableWidget()
        table.setAlternatingRowColors(True)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["Удалить", "Глава", "Позиция", "Блоков", "Серия", "Почему"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        if findings:
            table.setRowCount(len(findings))
            for row, finding in enumerate(findings):
                check_item = QTableWidgetItem("")
                check_item.setFlags(
                    QtCore.Qt.ItemFlag.ItemIsUserCheckable |
                    QtCore.Qt.ItemFlag.ItemIsEnabled |
                    QtCore.Qt.ItemFlag.ItemIsSelectable
                )
                check_item.setCheckState(QtCore.Qt.CheckState.Checked)
                check_item.setData(QtCore.Qt.ItemDataRole.UserRole, finding)
                table.setItem(row, 0, check_item)
                table.setItem(row, 1, QTableWidgetItem(f"{finding['chapter_index']}. {finding['chapter_name']}"))
                table.setItem(row, 2, QTableWidgetItem(finding['location']))
                table.setItem(row, 3, QTableWidgetItem(str(finding.get('block_count', 1))))
                table.setItem(row, 4, QTableWidgetItem(finding['text']))
                table.setItem(row, 5, QTableWidgetItem(finding['reason']))
        else:
            table.setRowCount(1)
            message_item = QTableWidgetItem(empty_text)
            message_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            table.setSpan(0, 0, 1, 6)
            table.setItem(0, 0, message_item)

        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setPlaceholderText("Выберите строку в таблице, чтобы увидеть контекст.")
        table.itemSelectionChanged.connect(lambda key=tab_key: self._update_preview_for_tab(key))

        tab_layout.addWidget(table, 1)
        tab_layout.addWidget(QLabel("Контекст:"))
        tab_layout.addWidget(preview, 1)

        self._tab_tables[tab_key] = table
        self._tab_previews[tab_key] = preview
        self.tabs.addTab(container, f"{tab_title} ({len(findings)})")

    def _update_preview_for_tab(self, tab_key):
        table = self._tab_tables.get(tab_key)
        preview = self._tab_previews.get(tab_key)
        if not table or not preview:
            return

        current_row = table.currentRow()
        if current_row < 0:
            preview.clear()
            return

        item = table.item(current_row, 0)
        finding = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        if not finding:
            preview.clear()
            return

        preview.setPlainText(finding.get('preview', ''))

    def _set_current_tab_checked(self, checked):
        current_widget = self.tabs.currentWidget()
        if current_widget is None:
            return

        for tab_key, table in self._tab_tables.items():
            if table.parentWidget() is current_widget:
                for row in range(table.rowCount()):
                    item = table.item(row, 0)
                    if item and item.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable:
                        item.setCheckState(
                            QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
                        )
                break

    def _collect_and_accept(self):
        selected = []
        seen = set()
        for table in self._tab_tables.values():
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if not item or item.checkState() != QtCore.Qt.CheckState.Checked:
                    continue
                finding = item.data(QtCore.Qt.ItemDataRole.UserRole)
                if not finding:
                    continue
                key = (
                    finding['chapter_path'],
                    tuple(tuple(path) for path in (finding.get('tag_paths') or [])),
                )
                if key in seen:
                    continue
                seen.add(key)
                selected.append(finding)

        if not selected:
            QMessageBox.information(self, "Нечего удалять", "Не отмечено ни одной серии дублей для удаления.")
            return

        self.selected_findings = selected
        self.accept()

    def get_selected_findings(self):
        return list(self.selected_findings)


class EpubAnalysisThread(QThread):
    analysis_finished = pyqtSignal(list)

    def __init__(self, virtual_epub_path, chapters_list, parent=None):
        super().__init__(parent)
        self.virtual_epub_path = virtual_epub_path
        self.chapters_list = chapters_list
        self.re_tag_opener = re.compile(r'<([a-zA-Z0-9]+)(\s+[^>]*)?>', re.IGNORECASE)
        self.re_attributes = re.compile(r'([a-zA-Z-]+)\s*=\s*["\']([^"\']*)["\']')
        self.re_br = re.compile(r'<br\b[^>]*>', re.IGNORECASE)
        # Regex для поиска заголовков
        self.re_h1 = re.compile(r'<h1\b[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL)
        self.re_title = re.compile(r'<title\b[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)

    def run(self):
        stats = {} 
        br_files_count = 0
        orphaned_text_count = 0
        num_mismatches = [] # Список проблем с нумерацией

        try:
            use_bs4 = 'bs4' in sys.modules
            # Подготовка культур для распознавания чисел
            cultures = []
            if RECOGNIZERS_AVAILABLE:
                cultures = [Culture.English, Culture.Chinese, Culture.Japanese]

            with open(self.virtual_epub_path, "rb") as epub_file, \
                    zipfile.ZipFile(epub_file, "r") as zf:
                for name in self.chapters_list:
                    try:
                        # 1. Анализ имени файла на наличие "чистого" номера
                        # Ищем одну группу цифр. Если их несколько (part_1_sec_2), пропускаем.
                        digits_groups = re.findall(r'\d+', os.path.basename(name))
                        target_number = None
                        if len(digits_groups) == 1:
                            target_number = int(digits_groups[0])
                        
                        content_bytes = zf.read(name)
                        content_str = content_bytes.decode('utf-8', errors='ignore')
                        
                        # --- АНАЛИЗ НУМЕРАЦИИ ---
                        if target_number is not None and RECOGNIZERS_AVAILABLE:
                            # Извлекаем текст заголовка (H1 приоритетнее Title)
                            header_text = ""
                            h1_match = self.re_h1.search(content_str)
                            if h1_match:
                                header_text = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
                            else:
                                title_match = self.re_title.search(content_str)
                                if title_match:
                                    header_text = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                            
                            if header_text:
                                found_match = False
                                for culture in cultures:
                                    results = recognize_number(header_text, culture)
                                    for res in results:
                                        if 'value' in res.resolution:
                                            val = res.resolution['value']
                                            # Если нашли число, и оно НЕ совпадает с именем файла
                                            if val != target_number:
                                                # Проверяем, может это просто "Часть 1" в главе 5?
                                                # Но если это ЕДИНСТВЕННОЕ или ПЕРВОЕ число в заголовке - это маркер.
                                                # Для безопасности считаем ошибкой, если в заголовке есть число,
                                                # которое не равно номеру файла, и нет числа, которое равно.
                                                all_nums_in_header = [r.resolution['value'] for r in results if 'value' in r.resolution]
                                                if target_number not in all_nums_in_header:
                                                    num_mismatches.append({
                                                        'type': 'num_mismatch',
                                                        'file': name,
                                                        'old_fragment': res.text, # Текст, который нужно заменить (напр. "Five")
                                                        'new_number': target_number,
                                                        'context': header_text
                                                    })
                                                    found_match = True
                                                    break
                                    if found_match: break

                        # --- ДАЛЕЕ СТАНДАРТНЫЙ АНАЛИЗ ---
                        
                        # 1. Проверка на <br>
                        if self.re_br.search(content_str):
                            br_files_count += 1
                        
                        # 2. Сбор статистики по тегам (атрибуты)
                        for match in self.re_tag_opener.finditer(content_str):
                            tag_name = match.group(1).lower()
                            attrs_str = match.group(2)
                            if tag_name not in ['p', 'div', 'span', 'body', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a', 'label']: continue
                            if tag_name not in stats: stats[tag_name] = {'total': 0, 'attrs': Counter()}
                            stats[tag_name]['total'] += 1
                            if attrs_str:
                                for attr_match in self.re_attributes.finditer(attrs_str):
                                    attr_name = attr_match.group(1).lower()
                                    attr_val = attr_match.group(2).strip()
                                    if attr_name in ['class', 'style'] and attr_val:
                                        stats[tag_name]['attrs'][f"{attr_name}={attr_val}"] += 1
                        
                        # 3. Проверка на сирот (код без изменений)
                        if use_bs4:
                            soup = BeautifulSoup(content_str, 'html.parser')
                            if soup.body:
                                for child in soup.body.children:
                                    if isinstance(child, NavigableString) and child.strip():
                                        orphaned_text_count += 1; break
                                    elif isinstance(child, Tag) and child.name in ['label', 'span', 'a', 'b', 'i', 'strong', 'em', 'img']:
                                        orphaned_text_count += 1; break

                    except Exception:
                        continue 

            # --- Формирование диагноза ---
            issues = []
            
            # А. Нумерация (НОВОЕ)
            if num_mismatches:
                # Группируем, чтобы не спамить
                issues.append({
                    'type': 'num_mismatch_group',
                    'count': len(num_mismatches),
                    'items': num_mismatches,
                    'desc': f"Рассинхрон нумерации: {len(num_mismatches)} глав имеют заголовок, не совпадающий с именем файла.\n(Пример: файл '05.xhtml', заголовок 'Глава Четвертая')"
                })

            if br_files_count > 0: issues.append({'type': 'br', 'count': br_files_count})
            if orphaned_text_count > 0: issues.append({'type': 'orphans', 'count': orphaned_text_count, 'desc': "Обнаружен текст и инлайн-теги вне абзацев."})

            THRESHOLD = 0.90
            for tag, data in stats.items():
                total = data['total']
                min_count = 1 if tag == 'label' else 5
                if total < min_count: continue
                for attr_key, count in data['attrs'].items():
                    if count / total >= THRESHOLD:
                        attr_name, attr_val = attr_key.split('=', 1)
                        issues.append({'type': 'attr', 'tag': tag, 'attr': attr_name, 'value': attr_val, 'percent': count / total})

            self.analysis_finished.emit(issues)

        except Exception as e:
            print(f"CRITICAL ERROR in analysis: {e}")
            self.analysis_finished.emit([])
            
        
class EpubCleanupOptionsDialog(QDialog):
    def __init__(self, issues, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Инструменты лечения EPUB")
        self.resize(650, 550)
        self.selected_fixes = [] 
        
        layout = QVBoxLayout(self)
        
        header = QLabel("Диагностическая карта")
        header_font = QtGui.QFont(); header_font.setBold(True); header_font.setPointSize(12)
        header.setFont(header_font); header.setStyleSheet(f"color: {theme_manager.color('danger')};")
        layout.addWidget(header)

        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        content_widget = QWidget()
        self.checks_layout = QVBoxLayout(content_widget)
        self.checkboxes = []

        # --- ГРУППА ПРИНУДИТЕЛЬНОГО ЛЕЧЕНИЯ ---
        force_group = QtWidgets.QGroupBox("Ручные операции (Принудительно)")
        force_layout = QVBoxLayout(force_group)
        
        self.cb_force_renumber = QtWidgets.QCheckBox("Сквозная перенумерация (1, 2, 3...)")
        self.cb_force_renumber.setToolTip(
            "Игнорирует текущие номера в файлах.\n"
            "Присваивает главам номера 1, 2, 3... в порядке их следования в книге.\n"
            "Обновляет заголовки и ссылки в оглавлении."
        )
        force_layout.addWidget(self.cb_force_renumber)
        self.checks_layout.addWidget(force_group)
        # ---------------------------------------

        if issues:
            self.checks_layout.addWidget(QLabel("<b>Найденные проблемы:</b>"))
            for issue in issues:
                if issue['type'] == 'num_mismatch_group':
                    text = f"🛠 {issue['desc']}\nБудут обновлены заголовки и ссылки."
                    cb = QtWidgets.QCheckBox(text)
                    cb.setProperty("issue_data_list", issue['items'])
                    self.checks_layout.addWidget(cb); self.checkboxes.append(cb)
                elif issue['type'] == 'br':
                    text = f"Жесткие разрывы строк (<br>). Найдено: {issue['count']}."
                    cb = QtWidgets.QCheckBox(text); cb.setChecked(True)
                    cb.setProperty("issue_data", issue)
                    self.checks_layout.addWidget(cb); self.checkboxes.append(cb)
                elif issue['type'] == 'orphans':
                    text = f"Текст вне абзацев (сироты): {issue['count']} файлов."
                    cb = QtWidgets.QCheckBox(text); cb.setChecked(True)
                    cb.setProperty("issue_data", issue)
                    self.checks_layout.addWidget(cb); self.checkboxes.append(cb)
                elif issue['type'] == 'attr':
                    percent = int(issue['percent'] * 100)
                    text = f"Мусорный атрибут {issue['attr']}='{issue['value']}' (<{issue['tag']}>): {percent}%"
                    cb = QtWidgets.QCheckBox(text); cb.setChecked(True)
                    cb.setProperty("issue_data", issue)
                    self.checks_layout.addWidget(cb); self.checkboxes.append(cb)
        else:
            self.checks_layout.addWidget(QLabel("<i>Автоматических проблем не найдено.</i>"))

        self.checks_layout.addStretch()
        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Выполнить")
        buttons.accepted.connect(self._collect_data_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _collect_data_and_accept(self):
        self.selected_fixes = []
        if self.cb_force_renumber.isChecked():
            self.selected_fixes.append({'type': 'force_renumber_sequential'})
        
        for cb in self.checkboxes:
            if cb.isChecked():
                items_list = cb.property("issue_data_list")
                if items_list: self.selected_fixes.extend(items_list)
                else: self.selected_fixes.append(cb.property("issue_data"))
        self.accept()

    def get_fixes(self):
        return self.selected_fixes


class EpubDeepCleanupTagRulesDialog(QDialog):
    def __init__(self, current_rules=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Правила глубокой очистки")
        self.setMinimumSize(700, 500)
        self.rules = dict(current_rules or get_default_deep_cleanup_tag_rules())
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        info_label = QLabel(
            "Настройте обработку тегов:\n"
            "keep: тег остается\n"
            "unwrap: тег снимается, содержимое сохраняется\n"
            "remove: тег удаляется; если включено сохранение, контент разворачивается"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        table_buttons = QHBoxLayout()
        reset_btn = QPushButton("Сбросить по умолчанию")
        reset_btn.clicked.connect(self._reset_to_defaults)
        keep_btn = QPushButton("Везде keep")
        keep_btn.clicked.connect(lambda: self._set_all_actions('keep'))
        unwrap_btn = QPushButton("Везде unwrap")
        unwrap_btn.clicked.connect(lambda: self._set_all_actions('unwrap'))
        table_buttons.addWidget(reset_btn)
        table_buttons.addWidget(keep_btn)
        table_buttons.addWidget(unwrap_btn)
        table_buttons.addStretch()
        layout.addLayout(table_buttons)

        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Тег", "Действие", "Сохранить контент"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(2, 150)
        layout.addWidget(self.table)
        self._fill_table()

        add_row = QHBoxLayout()
        self.new_tag_input = QtWidgets.QLineEdit()
        self.new_tag_input.setPlaceholderText("Новый тег...")
        add_btn = QPushButton("Добавить тег")
        add_btn.clicked.connect(self._add_tag)
        add_row.addWidget(self.new_tag_input)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_table(self):
        self.table.setRowCount(len(self.rules))
        for row, (tag, (action, preserve)) in enumerate(self.rules.items()):
            self.table.setItem(row, 0, QTableWidgetItem(tag))

            action_combo = QComboBox()
            action_combo.addItems(["keep", "remove", "unwrap"])
            action_combo.setCurrentText(action)
            self.table.setCellWidget(row, 1, action_combo)

            preserve_checkbox = QCheckBox()
            preserve_checkbox.setChecked(bool(preserve))
            preserve_checkbox.setEnabled(action == 'remove')
            action_combo.currentTextChanged.connect(
                lambda text, cb=preserve_checkbox: cb.setEnabled(text == 'remove')
            )
            preserve_wrapper = QWidget()
            preserve_layout = QHBoxLayout(preserve_wrapper)
            preserve_layout.setContentsMargins(0, 0, 0, 0)
            preserve_layout.addWidget(preserve_checkbox, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, 2, preserve_wrapper)

    def _reset_to_defaults(self):
        self.rules = get_default_deep_cleanup_tag_rules()
        self.table.clearContents()
        self._fill_table()

    def _set_all_actions(self, action):
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            if combo:
                combo.setCurrentText(action)

    def _add_tag(self):
        tag_name = self.new_tag_input.text().strip().lower()
        if not tag_name or tag_name in self.get_rules():
            return

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(tag_name))

        action_combo = QComboBox()
        action_combo.addItems(["keep", "remove", "unwrap"])
        self.table.setCellWidget(row, 1, action_combo)

        preserve_checkbox = QCheckBox()
        preserve_checkbox.setEnabled(False)
        preserve_wrapper = QWidget()
        preserve_layout = QHBoxLayout(preserve_wrapper)
        preserve_layout.setContentsMargins(0, 0, 0, 0)
        preserve_layout.addWidget(preserve_checkbox, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.table.setCellWidget(row, 2, preserve_wrapper)
        action_combo.currentTextChanged.connect(
            lambda text, cb=preserve_checkbox: cb.setEnabled(text == 'remove')
        )
        self.new_tag_input.clear()

    def get_rules(self):
        result = {}
        for row in range(self.table.rowCount()):
            tag_item = self.table.item(row, 0)
            if not tag_item:
                continue
            action_widget = self.table.cellWidget(row, 1)
            action = action_widget.currentText() if action_widget else 'keep'
            preserve_widget = self.table.cellWidget(row, 2)
            preserve_checkbox = preserve_widget.findChild(QCheckBox) if preserve_widget else None
            preserve = preserve_checkbox.isChecked() if preserve_checkbox else True
            result[tag_item.text().strip().lower()] = (action, preserve)
        return result


class EpubDeepCleanupOptionsDialog(QDialog):
    def __init__(self, current_settings=None, current_tag_rules=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Глубокая чистка EPUB")
        self.resize(560, 340)
        persisted_settings = load_deep_cleanup_settings()
        merged_settings = dict(persisted_settings)
        if isinstance(current_settings, dict):
            merged_settings.update({k: v for k, v in current_settings.items() if k != 'tag_rules'})

        base_rules = current_tag_rules or merged_settings.get('tag_rules') or get_default_deep_cleanup_tag_rules()
        self._settings_state = {
            'remove_css': bool(merged_settings.get('remove_css', True)),
            'remove_nav': bool(merged_settings.get('remove_nav', False)),
            'remove_fonts': bool(merged_settings.get('remove_fonts', True)),
            'apply_css_styles': bool(merged_settings.get('apply_css_styles', True)),
        }
        self._tag_rules = dict(normalize_deep_cleanup_tag_rules(base_rules))
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Этот режим переносит в приложение логику из внешнего EPUB-cleaner.\n"
            "Он может удалить CSS, nav-файлы, шрифты, развернуть теги и массово зачистить атрибуты."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.cb_remove_css = QCheckBox("Удалить все CSS из EPUB")
        self.cb_remove_nav = QCheckBox("Удалить nav.xhtml / nav.html")
        self.cb_remove_fonts = QCheckBox("Удалить встроенные шрифты")
        self.cb_apply_css = QCheckBox("Сначала перенести часть CSS-стилей в HTML-теги")
        self.cb_apply_css.setToolTip(
            "Пытается сохранить курсив, жирный, underline/strike и text-align:center "
            "перед удалением CSS."
        )
        self.cb_remove_css.setChecked(self._settings_state['remove_css'])
        self.cb_remove_nav.setChecked(self._settings_state['remove_nav'])
        self.cb_remove_fonts.setChecked(self._settings_state['remove_fonts'])
        self.cb_apply_css.setChecked(self._settings_state['apply_css_styles'])
        self.cb_remove_css.stateChanged.connect(self._persist_current_state)
        self.cb_remove_nav.stateChanged.connect(self._persist_current_state)
        self.cb_remove_fonts.stateChanged.connect(self._persist_current_state)
        self.cb_apply_css.stateChanged.connect(self._persist_current_state)

        layout.addWidget(self.cb_remove_css)
        layout.addWidget(self.cb_remove_nav)
        layout.addWidget(self.cb_remove_fonts)
        layout.addWidget(self.cb_apply_css)

        note = QLabel(
            "Резервная копия будет создана через стандартный механизм приложения "
            "при первой записи очищенного EPUB на диск."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme_manager.color('text_secondary')};")
        layout.addWidget(note)

        rules_box = QGroupBox("Правила тегов")
        rules_layout = QHBoxLayout(rules_box)
        self.rules_summary = QLabel()
        self.rules_summary.setWordWrap(True)
        self._refresh_rules_summary()
        edit_rules_btn = QPushButton("Настроить правила…")
        edit_rules_btn.clicked.connect(self._edit_tag_rules)
        rules_layout.addWidget(self.rules_summary, 1)
        rules_layout.addWidget(edit_rules_btn)
        layout.addWidget(rules_box)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Запустить")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _edit_tag_rules(self):
        dialog = EpubDeepCleanupTagRulesDialog(self._tag_rules, self)
        if exec_dialog(self, dialog) == QDialog.DialogCode.Accepted:
            self._tag_rules = dict(normalize_deep_cleanup_tag_rules(dialog.get_rules()))
            self._refresh_rules_summary()
            self._persist_current_state()

    def _refresh_rules_summary(self):
        unwrap_count = sum(1 for action, _ in self._tag_rules.values() if action == 'unwrap')
        remove_count = sum(1 for action, _ in self._tag_rules.values() if action == 'remove')
        keep_count = sum(1 for action, _ in self._tag_rules.values() if action == 'keep')
        self.rules_summary.setText(
            f"keep: {keep_count}, unwrap: {unwrap_count}, remove: {remove_count}. "
            f"Тегов в наборе: {len(self._tag_rules)}."
        )

    def get_options(self):
        return {
            'remove_css': self.cb_remove_css.isChecked(),
            'remove_nav': self.cb_remove_nav.isChecked(),
            'remove_fonts': self.cb_remove_fonts.isChecked(),
            'apply_css_styles': self.cb_apply_css.isChecked(),
            'tag_rules': dict(self._tag_rules),
        }

    def _persist_current_state(self, *_args):
        save_deep_cleanup_settings(self.get_options())
