# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# Утилиты для обработки текста
# ---------------------------------------------------------------------------
# Этот файл содержит функции для манипуляций с текстом, такие как
# разделение на части (чанкинг).
# ---------------------------------------------------------------------------

import re
import json
import html
import zlib
from lxml import etree
from bs4 import BeautifulSoup, NavigableString, Comment, Declaration, ProcessingInstruction
from itertools import groupby
from difflib import SequenceMatcher
from collections import Counter
import math
from ..api import config as api_config
from .glued_words import repair_glued_russian_words_in_html

DASH_CHARS = '—–−─-'
COMMA_CHARS = ',，'
ELLIPSIS_CHAR = '…'

# --- СИМВОЛЬНЫЕ НАБОРЫ (НОВАЯ СТРУКТУРА) ---
LOWERCASE_RU = 'а-яё'
UPPERCASE_RU = 'А-ЯЁ'
LOWERCASE_EN = 'a-z'
UPPERCASE_EN = 'A-Z'

# --- ИЕРАРХИЯ КАВЫЧЕК (ДЛЯ НОРМАЛИЗАЦИИ) ---
# Стратегия: "Геометрический ритм".
# Чередование: Углы -> Округлости -> Тонкие углы -> Тонкие округлости -> CJK

# Единый источник истины: [Открывающая, Закрывающая]
QUOTE_PAIRS = [
    ['«', '»'],  # 0: Классические елочки (Жирные углы)
    ['„', '“'],  # 1: Лапки (Жирные запятые)
    ['‹', '›'],  # 2: Одиночные елочки (Тонкие углы — режут массив)
    ['‘', '’'],  # 3: Английские одиночные (Тонкие запятые)
    ['〈', '〉'],  # 4: CJK Одиночные угловые (Легкая геометрия)
    ['“', '”'],  # 5: Английские двойные (Возврат веса перед CJK)
    ['《', '》'],  # 6: CJK Двойные угловые
    ['『', '』'],  # 7: CJK Белые уголки
    ['「', '」'],  # 8: CJK Уголки
    ['"', '"']    # 9: Грязная прямая кавычка (Must have для очистки)
]

# Автоматическая генерация списков (Гарантия синхронизации)
QUOTE_HIERARCHY_OPEN = [pair[0] for pair in QUOTE_PAIRS]
QUOTE_HIERARCHY_CLOSE = [pair[1] for pair in QUOTE_PAIRS]


# Автоматическая генерация наборов для поиска (Гарантия синхронизации)
DETECT_OPEN_QUOTES = "".join(QUOTE_HIERARCHY_OPEN)
DETECT_CLOSE_QUOTES = "".join(QUOTE_HIERARCHY_CLOSE)
ALL_QUOTES = DETECT_OPEN_QUOTES + DETECT_CLOSE_QUOTES


# --- ОБЪЕДИНЕННЫЕ НАБОРЫ ДЛЯ РЕГУЛЯРНЫХ ВЫРАЖЕНИЙ ---
# Все строчные буквы (русские и латинские)
LOWERCASE_CHARS = f'{LOWERCASE_RU}{LOWERCASE_EN}'
# Все заглавные буквы (русские и латинские)
UPPERCASE_CHARS = f'{UPPERCASE_RU}{UPPERCASE_EN}'
# Все буквы любого регистра
ALL_LETTER_CHARS = f'{LOWERCASE_CHARS}{UPPERCASE_CHARS}'

TAGS_FOR_NEW_LINE = [
    "</p>", "</div>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>",
    "</li>", "</blockquote>", "</pre>", "</tr>", "</th>", "</td>"
]

SPLIT_DASH = r'\s*</p>\s*<p[^>]*>\s*' # 'r' можно ставить до или после 'f'
TAG_STRIPPER = re.compile(r'<[^>]+>')

CJK_UNSPACED_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff]')
SPACED_SCRIPTS_RE = re.compile(r'[a-zA-Zа-яА-Я\uac00-\ud7a3]')

BR_HR_PATTERN = re.compile(r'<(br|hr)\s*/?>', flags=re.IGNORECASE)
NEWLINES_PATTERN = re.compile(r'(\s*\n\s*){3,}')
PUNCTUATION_CLEANUP_PATTERN = re.compile(r'[?!]{2,}')
CLEANUP_PATTERN = re.compile(
    # Находим необязательные пробелы до запятой
    fr'\s*'
    # Находим одну или несколько запятых
    fr'[{COMMA_CHARS}]+'
    # Находим необязательные пробелы после запятой, но перед троеточием
    fr'\s*'
    # Находим само троеточие (оно становится частью совпадения)
    fr'{ELLIPSIS_CHAR}'
    # Находим необязательные пробелы ПОСЛЕ троеточия
    fr'\s*'
)
END_DASH_TO_ELLIPSIS_PATTERN = re.compile(fr'\s*[{DASH_CHARS}]+\s*</p>')
TAG_NEWLINE_PATTERN = re.compile(r'(</(p|h1|div)>)')
COMMA_MERGE_PATTERN = re.compile(fr'([{COMMA_CHARS}])\s*</p>\s*<p[^>]*>')
END_DASH_CANDIDATE_PATTERN = re.compile(fr'\s*[{DASH_CHARS}]+\s*</p>')
FORBIDDEN_CHARS_BEFORE_DASH = ALL_LETTER_CHARS + COMMA_CHARS

# Находит ЛЮБОЕ тире (или группу), которое упирается в знак препинания.
# Включает: точку, запятую (все виды), двоеточие, точку с запятой, вопросы, восклицания и закрывающую кавычку.
# Игнорирует открывающие кавычки.
DASH_BEFORE_PUNCTUATION_PATTERN = re.compile(
    fr'\s*[{DASH_CHARS}]+\s*'       # Захват тире и пробелов вокруг
    fr'(?=[{COMMA_CHARS}.:;?!»])'   # Lookahead: дальше идет знак препинания
)

# Чистит артефакт "…." (троеточие + точка), который образуется после замены "—." -> "….".
# Превращает его в чистое троеточие "…". Остальные знаки (…, …? …!) легальны.
ELLIPSIS_DOT_CLEANUP_PATTERN = re.compile(fr'{ELLIPSIS_CHAR}\s*\.')

# --- ПАТТЕРН РАЗРЫВА ДИАЛОГОВ ---
# Добавлено \s* перед lookahead. 
# Это "съедает" все пробелы после двоеточия, чтобы <p> приклеился к тире вплотную.
DIALOGUE_SPLIT_PATTERN = re.compile(
    fr'(<p[^>]*>)'                                 # Группа 1: Открывающий тег
    fr'((?:(?!</p>).)*?)'                          # Группа 2: Текст автора
    fr'(:)'                                        # Группа 3: Двоеточие
    fr'\s*'                                        # <--- ВАЖНО: Съедаем пробелы после двоеточия
    fr'(?=[{DASH_CHARS}]+\s*[{UPPERCASE_CHARS}])'  # Lookahead: Дальше тире и Большая буква
)


def _mask_html_tags(text: str, token_prefix: str):
    tag_map = {}

    def mask_callback(match: re.Match) -> str:
        key = f"\0{token_prefix}_{len(tag_map)}\0"
        tag_map[key] = match.group(0)
        return key

    return re.sub(r'<[^>]+>', mask_callback, text), tag_map


# Ключи масок имеют вид "\0PREFIX_N\0" (TAG/B_TAG/TAG_INIT/TAG_FIN).
_MASK_TOKEN_PATTERN = re.compile(r'\0[A-Za-z0-9_]+\0')


def _restore_masked_segments(text: str, segment_map: dict[str, str]) -> str:
    # Один проход вместо str.replace на каждый тег: на главе с сотнями
    # тегов старый цикл копировал весь текст на каждой итерации.
    if not segment_map:
        return text
    return _MASK_TOKEN_PATTERN.sub(
        lambda m: segment_map.get(m.group(0), m.group(0)), text
    )

DASH_TO_ELLIPSIS_PATTERN = re.compile(
    # ЗАХВАТЫВАЕМ (Группа 1): тире и пробелы вокруг него, которые нужно заменить
    fr'(\s*[{DASH_CHARS}]+\s*)'
    
    # УСЛОВИЕ (Просмотр вперёд): дальше должен быть разрыв абзаца, за которым
    # следует либо (ещё одно тире) ИЛИ (заглавная буква)
    fr'(?=\s*</p>\s*<p[^>]*>\s*'
    fr'(?:[{DASH_CHARS}]|[{UPPERCASE_CHARS}]))'
)


# --- "УМНЫЕ" ПАТТЕРНЫ СЛИЯНИЯ (ФИНАЛЬНАЯ, ОТЛАЖЕННАЯ ВЕРСИЯ) ---
REMARK_MERGE_PATTERN = re.compile(
    # ЗАХВАТЫВАЕМ только запятую (Группа 1)
    fr'([{COMMA_CHARS}]+)'
    
    # НАХОДИМ пробелы (но не захватываем)
    fr'\s*'
    
    # ЗАХВАТЫВАЕМ только ПЕРВОЕ тире (Группа 2)
    fr'([{DASH_CHARS}]+)'
    
    # НАХОДИМ (для удаления) разрыв и ВТОРОЕ тире
    fr'{SPLIT_DASH}[{DASH_CHARS}]+\s*'
    
    # УСЛОВИЕ: дальше строчная буква
    fr'(?=[{LOWERCASE_CHARS}])'
)

COMMA_DASH_MERGE_PATTERN = re.compile(
    # ЗАХВАТЫВАЕМ только запятую (Группа 1)
    fr'([{COMMA_CHARS}]+)'
    
    # НАХОДИМ пробелы (но не захватываем)
    fr'\s*'
    
    # ЗАХВАТЫВАЕМ только тире (Группа 2)
    fr'([{DASH_CHARS}]+)'
    
    # НАХОДИМ разрыв абзаца (включая пробелы вокруг него)
    fr'{SPLIT_DASH}'
    
    # УСЛОВИЕ: следующий абзац начинается со строчной буквы
    fr'(?=[{LOWERCASE_CHARS}])'
)

FINAL_MERGE_PATTERN = re.compile(
    # НАХОДИМ разрыв
    fr'{SPLIT_DASH}\s*'
    fr'([{DASH_CHARS}]+\s*)'
    # УСЛОВИЕ: дальше строчная буква
    fr'(?=[{LOWERCASE_CHARS}])' # <-- ОБНОВЛЕНО
)

SUB_MERGE_PATTERN = re.compile(
    fr'\s*'
    fr'([{DASH_CHARS}])'
    fr'\s*{SPLIT_DASH}\s*'
    fr'(?=[{LOWERCASE_CHARS}])' # <-- ОБНОВЛЕНО
)

COMMA_BEFORE_DIALOGUE_MERGE_PATTERN = re.compile(
    fr'([{COMMA_CHARS}]+)'
    fr'({SPLIT_DASH})'
    fr'([{DASH_CHARS}]+)'
    fr'\s*'
    fr'(?=[{LOWERCASE_CHARS}])' # <-- ОБНОВЛЕНО
)

COMMA_LOWERCASE_MERGE_PATTERN = re.compile(
    f'([{COMMA_CHARS}])'
    f'{SPLIT_DASH}'
    f'(?=[{LOWERCASE_CHARS}])' # <-- ОБНОВЛЕНО
)

LETTER_MERGE_PATTERN = re.compile(
    f'(?<=[{ALL_LETTER_CHARS}])' # <-- ОБНОВЛЕНО
    f'{SPLIT_DASH}'
    f'(?=[{LOWERCASE_CHARS}])' # <-- ОБНОВЛЕНО
)


# Находим знаки препинания (.,:;?!), после которых забыт пробел,
# но только если дальше идет текст, тире или открытие новой конструкции.
MISSING_SPACE_PATTERN = re.compile(
    fr'([{COMMA_CHARS}.:;?!])'                    # Знак препинания
    fr'(?=[{ALL_LETTER_CHARS}«(\[]{DASH_CHARS}])' # Lookahead: буква, кавычка... и ТИРЕ. Скобка закрывается в конце.
)


# Находит ЛЮБОЙ открытый <p>, который не закрылся до следующего <p> ИЛИ до конца родительского блока
# Находит <p>, который не закрыт перед следующим блочным тегом
MISSING_CLOSE_PATTERN = re.compile(
    r'(<p(?:\s+[^>]*)?>)'                   # Группа 1: Открывающий P
    r'((?:(?!</p>).)*?)'                    # Группа 2: Контент
    r'(?=<p(?:\s|>)|</div|</body|</section|</article|</h[1-6])', 
    re.DOTALL | re.IGNORECASE
)
# Находит текст, который оказался за пределами <p> внутри структуры
MISSING_OPEN_PATTERN = re.compile(
    r'(</p>|<body>|<div>|<blockquote>)'      # Группа 1: Предыдущий блок
    r'(\s*[^<\s][^<]*?)'                    # Группа 2: Контент (минимум один значимый символ!)
    r'(?=</p>|</div>|</body>|<blockquote>)', # Lookahead: граница
    re.DOTALL | re.IGNORECASE
)
# Паттерн для поиска атрибутов предыдущего параграфа
P_ATTR_SEARCH = re.compile(r'<p(\s+[^>]*?)?>', re.IGNORECASE)

# Находит символ '&', за которым НЕ следует валидная сущность (например, lt;, amp;, #123;)
# Это лечит ошибки вида "Т&И" -> "Т&amp;И"
RAW_AMPERSAND_PATTERN = re.compile(r'&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#\d+|#x[0-9a-fA-F]+);)')

# --- MARKDOWN CLEANUP PATTERNS ---
# 1. Проверка на сепаратор: строка состоит ТОЛЬКО из звезд, пробелов и тире
IS_SEPARATOR_PATTERN = re.compile(r'^[\s*—–−_+=#-]+$')

# 2. Поиск текстового контента между тегами: (>)(контент)(<)
TEXT_NODE_PATTERN = re.compile(r'(>)([^<]+)(<)')

# 3. Валидные Markdown пары
MD_BOLD_ITALIC_PATTERN = re.compile(r'(?<!\*)\*\*\*([^\*\n]+?)\*\*\*(?!\*)')
MD_BOLD_PATTERN = re.compile(r'(?<!\*)\*\*([^\*\n]+?)\*\*(?!\*)')
MD_ITALIC_PATTERN = re.compile(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)')

# 4. Мусорные звезды (одиночки на границах)
# Удаляем звезды, если у них есть пробел (или граница строки) хотя бы с одной стороны.
# (?<!\S) - слева пустота или пробел.
# (?!\S)  - справа пустота или пробел.
MD_GARBAGE_STARS_PATTERN = re.compile(r'(?<!\S)\*+|\*+(?!\S)')
# ---------------------------------------------------------------

# 1. Анализ НАЧАЛА абзаца
START_DASH_PATTERN = re.compile(fr'^(\s*(?:<[^>]+>\s*)*)([{DASH_CHARS}]+)')
# 2. Токенизация и проход
TOKEN_PATTERN = re.compile(
    fr'(\0B_TAG_\d+\0)|([{ALL_QUOTES}])|([{DASH_CHARS}]+)'
    .replace('{DASH_CHARS}', DASH_CHARS)
)

# Находит тире (любого вида) сразу после открывающей кавычки (с пробелами или без).
# « — Текст -> «… Текст
# «-Текст -> «…Текст
START_QUOTE_DASH_PATTERN = re.compile(fr'([«„])\s*[{DASH_CHARS}]+')

# --- ПАТТЕРНЫ ДЛЯ ЛОГИКИ РАЗДЕЛЕНИЯ И ФИНАЛИЗАЦИИ ---

# 1. FIX CAPITALIZATION (БАРЬЕР СЛИЯНИЯ)
# Находит ситуацию: Конец предложения (.?!…) -> Граница абзацев -> (Тире) -> Строчная буква.
# Группа 1: Терминатор + возм. кавычка
# Группа 2: Граница тегов
# Группа 3: Опциональное тире и пробелы
# Группа 4: Строчная буква
CAPITALIZATION_FIX_PATTERN = re.compile(
    fr'([.?!…][»“"”]?)\s*'       
    fr'(</p>\s*<p[^>]*>\s*)'     
    fr'((?:[{DASH_CHARS}]+\s*)?)' 
    fr'([{LOWERCASE_CHARS}])'    
)

# 2. FIX END PUNCTUATION (ЧИСТОТА КОНЦОВКИ)
# Запятая в конце абзаца (перед опциональной кавычкой и </p>)
END_COMMA_FIX_PATTERN = re.compile(fr',\s*(?=[»“"”]*\s*</p>)')

# Отсутствие знака препинания в конце абзаца (Буква/Цифра -> Кавычка -> </p>)
# Исключает ситуации, когда знак уже есть.
MISSING_DOT_PATTERN = re.compile(fr'(?<=[{ALL_LETTER_CHARS}0-9])(?=[»“"”]*\s*</p>)')

# Находит " это" (с пробелом перед ним) и границей слова после.
# Регистр неважен (ЭТО, это, Это).
ETO_LOOKAHEAD_PATTERN = re.compile(r'^\s*это\b', re.IGNORECASE)

# Соединяет диалог, разорванный после знака препинания.
# Включает логику: Точка -> Запятая, Троеточие -> Троеточие.
# Игнорирует случаи, если после тире идет что-то кроме буквы (например "— ..поспешила").
# Используем глобальный набор закрывающих кавычек вместо хардкода.
PUNCTUATION_DASH_MERGE_PATTERN = re.compile(
    fr'([.?!…][{DETECT_CLOSE_QUOTES}]?)' # Группа 1: Знак + опц. закрывающая кавычка из набора
    fr'{SPLIT_DASH}'                      # Разрыв абзацев
    fr'([{DASH_CHARS}]+)'                 # Группа 2: Тире
    fr'\s*'                               # Пробелы (съедаем)
    fr'(?=[{LOWERCASE_CHARS}])'           # Lookahead: Дальше ОБЯЗАНА быть строчная буква
)

CAPITALIZE_PATTERN = fr'(?<!{ELLIPSIS_CHAR})(?<!\.)([!?.])(?!(?:{ELLIPSIS_CHAR}|\.))(\s*(?:[{DASH_CHARS}]\s*)?)([{LOWERCASE_CHARS}])'

# Список тегов, которые считаются блочными барьерами
BLOCK_TAGS_PATTERN = re.compile(r'^</?(p|div|h[1-6]|li|blockquote|br|hr|ul|ol|table|tr|td|th)\b', re.IGNORECASE)

# --- ПЫЛЕСОС ТОЧЕК (DOTS VACUUM) ---
# Находит любые комбинации точек и троеточий, идущие подряд.
# Условие {2,} означает "2 или больше".
# .. -> …
# …. -> …
# …... -> …
# . -> . (не трогает обычную точку)
ELLIPSIS_CHAOS_PATTERN = re.compile(r'(?:[.]|…){2,}')

GLOBAL_OP_PATTERN = re.compile(r'[,](?:\s|\0(?:B_)?TAG_\d+\0)*─')
    
def dialogue_splitter_with_attributes(match: re.Match) -> str:
    opening_tag = match.group(1)
    text_before_colon = match.group(2)
    colon = match.group(3)
    return f"{opening_tag}{text_before_colon}{colon}</p>{opening_tag}"

def ellipsis_replacer(match: re.Match) -> str:
    """
    Заменяет найденное тире на троеточие.
    Добавляет пробел после, только если за тире не следует тег </p>.
    """
    # Получаем весь текст ПОСЛЕ найденного тире
    text_after = match.string[match.end():]
    
    # Проверяем, начинается ли этот текст с пробелов, за которыми идет </p>
    # re.match проверяет строку с самого начала
    if re.match(r'\s*</p>', text_after):
        # Если да, то пробел не нужен. Возвращаем только троеточие.
        return ELLIPSIS_CHAR
    else:
        # Во всех остальных случаях добавляем пробел.
        return f'{ELLIPSIS_CHAR} '
        
def smart_end_dash_replacer(match: re.Match) -> str:
    """
    Интеллектуальная замена тире в конце абзаца.
    Удаляет тире, только если последний значимый символ перед ним
    не является буквой или запятой.
    """
    # 1. Берем весь текст до найденного тире
    text_before = match.string[:match.start()]

    # 2. Убираем из этого текста все HTML-теги
    text_only = TAG_STRIPPER.sub("", text_before)

    # 3. Убираем пробельные символы в конце
    stripped_text_only = text_only.rstrip()

    # Если после очистки ничего не осталось (например, <p><em></em>—</p>)
    if not stripped_text_only:
        return "</p>"  # Удаляем тире

    # 4. Смотрим на самый последний СИМВОЛ ТЕКСТА
    last_char = stripped_text_only[-1]

    # 5. Проверяем, является ли он "запрещенным" (буква или запятая)
    if re.fullmatch(f'[{FORBIDDEN_CHARS_BEFORE_DASH}]', last_char):
        # Если да - ничего не меняем, возвращаем тире как было
        return match.group(0)
    else:
        # Если нет (кавычка, точка и т.д.) - удаляем тире
        return "</p>"
    
def merge_dialogue_punctuation(match: re.Match) -> str:
    """
    Обработчик слияния диалогов.
    Логика:
    1. "Фраза." + "— сказал" -> "Фраза, — сказал"
    2. "Фраза?" + "— сказал" -> "Фраза? — сказал"
    3. "Фраза..." + "— сказал" -> "Фраза... — сказал"
    """
    end_punct_with_quote = match.group(1)
    dash = match.group(2)
    
    # Если в конце есть троеточие (…), вопрос (?) или восклицание (!) — оставляем как есть.
    # Проверка на '...' добавлена на всякий случай, если ELLIPSIS_CHAOS еще не отработал.
    if '…' in end_punct_with_quote or '...' in end_punct_with_quote or '?' in end_punct_with_quote or '!' in end_punct_with_quote:
        return f"{end_punct_with_quote} {dash} "
        
    # Если мы здесь, значит это точка (или точка с кавычкой).
    # Меняем точку на запятую.
    return f"{end_punct_with_quote.replace('.', ',')} {dash} "
    
def normalize_punctuation(match: re.Match) -> str:
    """
    Нормализует последовательности '!' и '?', сохраняя первый символ
    и добавляя второй (либо "другой", либо дубликат первого).
    """
    sequence = match.group(0)
    if not sequence:
        return ""

    first_char = sequence[0]
    # Определяем, какой символ является "другим"
    other_char = '?' if first_char == '!' else '!'
    
    # Если "другой" символ присутствует в остатке строки, используем его.
    # В противном случае, просто дублируем первый.
    second_char = other_char if other_char in sequence[1:] else first_char
            
    return first_char + second_char

def capitalize_next_paragraph_start(match: re.Match) -> str:
    """
    Поднимает регистр первой буквы нового абзаца, если предыдущий
    закончился терминатором (.?!). Это предотвращает ошибочное склеивание.
    """
    terminator = match.group(1)
    boundary = match.group(2)
    dash_part = match.group(3) or "" # Может быть None, если тире нет
    char = match.group(4)
    
    return f"{terminator}{boundary}{dash_part}{char.upper()}"


def cleanup_replacer(match: re.Match) -> str:
    """
    Заменяет ",..." на "..." с умным добавлением пробела.
    Пробел добавляется только в том случае, если за конструкцией не следует
    закрывающий тег </p>.
    """
    # Смотрим на текст, который идёт СРАЗУ ПОСЛЕ всего нашего совпадения
    text_after = match.string[match.end():]

    # Если этот текст начинается с </p> (с учётом возможных пробелов)...
    if re.match(r'\s*</p>', text_after):
        # ...то мы находимся в конце абзаца. Пробел НЕ НУЖЕН.
        return ELLIPSIS_CHAR
    else:
        # ...в противном случае, мы в середине предложения. Пробел НУЖЕН.
        return f'{ELLIPSIS_CHAR} '


def is_well_formed_xml(content: str, validate=False) -> bool:
    """Проверяет, является ли строка валидным XML, используя строгий парсер lxml.etree."""
    try:
        # etree.fromstring выбрасывает исключение при малейшем нарушении структуры XML
        etree.fromstring(content.encode('utf-8'))
        if validate:
            return True, None
        return True
    except etree.XMLSyntaxError as e:
        if validate:
            return False, str(e)
        return False


XHTML_TAG_CASE_NORMALIZE_NAMES = {
    'a', 'abbr', 'address', 'area', 'article', 'aside', 'audio',
    'b', 'base', 'bdi', 'bdo', 'blockquote', 'body', 'br', 'button',
    'caption', 'cite', 'code', 'col', 'colgroup',
    'data', 'dd', 'del', 'details', 'dfn', 'dialog', 'div', 'dl', 'dt',
    'em', 'figcaption', 'figure', 'footer', 'form',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head', 'header', 'hr', 'html',
    'i', 'img', 'input', 'ins', 'kbd', 'label', 'legend', 'li', 'link',
    'main', 'mark', 'meta', 'nav', 'ol', 'option', 'p', 'pre', 'q',
    'rp', 'rt', 'ruby', 's', 'samp', 'script', 'section', 'select',
    'small', 'source', 'span', 'strong', 'style', 'sub', 'summary',
    'sup', 'table', 'tbody', 'td', 'textarea', 'tfoot', 'th', 'thead',
    'time', 'title', 'tr', 'track', 'u', 'ul', 'var', 'video',
}


def normalize_xhtml_tag_case(html_content: str) -> str:
    """
    Normalize known XHTML tag names to lowercase without touching attributes,
    XML declarations, doctypes, comments, or unknown/custom tags.
    """
    if not isinstance(html_content, str) or '<' not in html_content:
        return html_content

    tag_pattern = re.compile(
        r'<\s*(/?)\s*([A-Za-z][A-Za-z0-9:_-]*)([^<>]*?)(/?)\s*>',
        flags=re.DOTALL,
    )

    def replace_tag(match: re.Match) -> str:
        slash, tag_name, attrs, self_close = match.groups()
        normalized_name = tag_name.lower()
        local_name = normalized_name.split(':', 1)[-1]
        if local_name not in XHTML_TAG_CASE_NORMALIZE_NAMES:
            return match.group(0)
        if slash:
            return f"</{normalized_name}>"
        return f"<{normalized_name}{attrs}{self_close}>"

    return tag_pattern.sub(replace_tag, html_content)


SPEECH_OPERATOR = '─'   # ─ BOX DRAWINGS LIGHT HORIZONTAL
EN_DASH = '–'           # –
EM_DASH = '—'           # — запрещён в выводе

DASH_BLOCK_TAGS = {
    'p', 'div', 'li', 'blockquote', 'dd', 'dt', 'figcaption',
    'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
}
_ATTRIBUTION_TAIL_RE = re.compile(r'[,!?…]\s*$')


def _normalize_block_dashes(block) -> None:
    """Заменяет — внутри одного блока на ─ (граница речи) или – (всё остальное)."""
    nodes = [
        node for node in block.descendants
        if isinstance(node, NavigableString) and not isinstance(node, Comment)
    ]
    if not nodes:
        return

    joined = "".join(str(node) for node in nodes)
    if EM_DASH not in joined:
        return

    leading = joined.lstrip()
    is_speech_block = bool(leading) and leading[0] in (SPEECH_OPERATOR, EM_DASH)

    chars = list(joined)
    for index, char in enumerate(chars):
        if char != EM_DASH:
            continue
        prefix = joined[:index]
        if not prefix.strip():
            # Тире в начале блока — это открытие реплики.
            chars[index] = SPEECH_OPERATOR
        elif is_speech_block and _ATTRIBUTION_TAIL_RE.search(prefix):
            # «…реплика, — слова автора»: переход [SOUND] -> [SILENCE].
            chars[index] = SPEECH_OPERATOR
        else:
            chars[index] = EN_DASH

    # Замены посимвольные, длина сохраняется, поэтому режем строго по узлам.
    normalized = "".join(chars)
    offset = 0
    for node in nodes:
        length = len(str(node))
        node.replace_with(NavigableString(normalized[offset:offset + length]))
        offset += length


def normalize_dialogue_dashes(html_content: str) -> str:
    """
    Приводит тире к правилам проекта: ─ только на границе речи и авторского
    текста, во всех остальных случаях –. Em-dash (—) в выводе не остаётся.
    Атрибуты, комментарии и служебные маркеры не затрагиваются.
    """
    if not isinstance(html_content, str) or EM_DASH not in html_content:
        return html_content

    soup = BeautifulSoup(html_content, 'html.parser')
    blocks = [
        tag for tag in soup.find_all(DASH_BLOCK_TAGS)
        if not tag.find(DASH_BLOCK_TAGS)  # только внутренние блоки, без двойной обработки
    ]
    for block in (blocks or [soup]):
        _normalize_block_dashes(block)

    return str(soup)


_FIRST_HEADING_RE = re.compile(
    r'(<h([1-6])\b[^>]*>)(?P<text>[^<]*)(</h\2\s*>)',
    flags=re.IGNORECASE,
)
_NUMBERED_CHAPTER_RE = re.compile(r'^\s*Глава\s+(\d+)\s*[:.]?\s*(.*?)\s*$')
_UNNUMBERED_CHAPTER_RE = re.compile(r'^\s*Глава\s*[:.]\s*(.+?)\s*$')


def _format_chapter_title(raw_title: str) -> str:
    title = raw_title.strip()
    if title.startswith('«') and title.endswith('»'):
        title = title[1:-1].strip()
    title = title.replace('«', '„').replace('»', '“')
    return f'«{title}»'


def _normalized_chapter_heading_text(original_text: str) -> str | None:
    match = _NUMBERED_CHAPTER_RE.match(original_text)
    if match:
        number, raw_title = match.groups()
    else:
        match = _UNNUMBERED_CHAPTER_RE.match(original_text)
        if not match:
            return None
        number, raw_title = None, match.group(1)

    # «Глава 165» без названия оставляем как есть — оборачивать нечего.
    if not raw_title.strip():
        return None

    prefix = f'Глава {number}: ' if number else 'Глава: '
    return prefix + _format_chapter_title(raw_title)


def normalize_chapter_heading_format(html_content: str) -> str:
    """
    Приводит первый заголовок к стандарту проекта: `Глава N: «Название»`.
    Вложенные кавычки становятся `„…“`. Заголовки без слова «Глава» (в том
    числе непереведённые) и заголовки без названия остаются как есть.
    Работает регуляркой, чтобы не платить лишним разбором HTML.
    """
    if not isinstance(html_content, str) or 'Глава' not in html_content:
        return html_content

    match = _FIRST_HEADING_RE.search(html_content)
    if not match:
        return html_content

    original_text = match.group('text')
    new_text = _normalized_chapter_heading_text(original_text)
    if new_text is None or new_text == original_text.strip():
        return html_content

    start, end = match.span('text')
    return html_content[:start] + new_text + html_content[end:]


def oper_dash_symbol(html_content: str) -> str:

    
    content = html_content
    
    
    # 3. ЛОГИКА "КИТАЙСКОГО" ТИРЕ (BOX DRAWINGS ─)
    # Порядок замен КРИТИЧЕСКИ ВАЖЕН.
    
    # СЛУЧАЙ А: Троеточие или куча точек (.. ... ….).
    # «Огонь..» ─ -> «Огонь…», ─
    # Ловим любую комбинацию точек/троеточий (2+ символа) ИЛИ одиночное троеточие.
    # Превращаем в чистое … внутри, запятую снаружи.
    content = re.sub(r'((?:[.]|…){2,}|…)\s*([»“"”])\s*─', r'…\2, ─', content)
    
    # СЛУЧАЙ Б: Восклицательный или Вопросительный знак.
    # «Огонь!» ─ -> «Огонь!», ─
    # Знак сохраняем, запятую добавляем.
    content = re.sub(r'([?!])\s*([»“"”])\s*─', r'\1\2, ─', content)
    
    # СЛУЧАЙ В: Одиночная точка.
    # «Огонь.» ─ -> «Огонь», ─
    # Точку удаляем, запятую ставим.
    # Lookbehind (?<![.]) гарантирует, что мы не откусим кусок от .. (хотя Случай А должен был их перехватить).
    content = re.sub(r'(?<![.])\.\s*([»“"”])\s*─', r'\1, ─', content)
    
    # СЛУЧАЙ Г: Нет знака (Буква или Цифра).
    # «Огонь» ─ -> «Огонь», ─
    # Lookbehind проверяет, что перед кавычкой буква, цифра или закрывающая скобка.
    # Саму букву не трогаем, просто добавляем запятую после кавычки.
    content = re.sub(r'(?<=[а-яА-ЯёЁa-zA-Z0-9)])\s*([»“"”])\s*─', r'\1, ─', content)
    
    return content
    
def clean_glossary_garbage(html_content):
    """
    Удаляет теги оформления (strong, b, em, i, span), которые ИИ использует
    для выделения терминов глоссария.
    Логика:
    1. Находит все строчные теги внутри <p>.
    2. Группирует содержимое по схожести текста (> 70%).
    3. Если в группе 2 и более элемента — сносит теги, оставляя текст.
    """
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Список тегов, которые "подозреваются" в оформлении
    # Можно добавить 'u', 'mark' и т.д.
    SUSPICIOUS_TAGS = ['strong', 'b', 'em', 'i', 'span']
    
    # Сюда соберем кандидатов: (объект_тега, чистый_текст)
    candidates = []
    
    # 1. СБОР ВСЕХ КАНДИДАТОВ
    for tag_name in SUSPICIOUS_TAGS:
        for tag in soup.find_all(tag_name):
            # Проверяем, что родитель - это абзац (P)
            # Это гарантирует условие "открылись и закрылись в том же абзаце"
            if tag.parent and tag.parent.name == 'p':
                
                # Получаем текст внутри тега
                text = tag.get_text(strip=True)
                
                # Игнорируем пустые теги или слишком длинные предложения 
                # (термины обычно короткие, до 6-7 слов)
                if not text or len(text.split()) > 7:
                    continue
                
                candidates.append({
                    'tag': tag,
                    'text': text,
                    'processed': False # Флаг, чтобы не обрабатывать дважды
                })

    # Если кандидатов нет, возвращаем как есть
    if not candidates:
        return html_content

    # 2. ГРУППИРОВКА ПО СХОЖЕСТИ (Similarity > 0.7)
    # Мы не можем использовать Counter напрямую для нечеткого поиска,
    # поэтому создаем группы вручную.
    
    groups = [] # Список списков индексов [ [0, 5, 8], [1, 2], ... ]
    
    # Вспомогательная функция для проверки схожести
    def is_similar(a, b, threshold=0.7):
        # Сначала быстрая проверка на полное совпадение
        if a.lower() == b.lower():
            return True
        # Медленная проверка через SequenceMatcher
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold

    indices = list(range(len(candidates)))
    
    while indices:
        current_idx = indices.pop(0)
        current_text = candidates[current_idx]['text']
        
        # Создаем новую группу с текущим элементом
        current_group = [current_idx]
        
        # Ищем похожих среди оставшихся
        # Идем с конца, чтобы безопасно удалять из списка indices
        for i in range(len(indices) - 1, -1, -1):
            other_idx = indices[i]
            other_text = candidates[other_idx]['text']
            
            if is_similar(current_text, other_text):
                current_group.append(other_idx)
                indices.pop(i) # Удаляем из общего пула, так как он уже в группе
        
        groups.append(current_group)

    # 3. УДАЛЕНИЕ ТЕГОВ (UNWRAP)
    # Логика: если в группе 2 и более элемента ИЛИ
    # (дополнительная эвристика) если тег очень похож на термин (короткий, с большой буквы), 
    # а ИИ в этом тексте в принципе злоупотребляет этим тегом.
    
    # Считаем, какие теги вообще чаще всего используются для мусора
    tag_names_in_garbage = [candidates[idx]['tag'].name for group in groups if len(group) >= 2 for idx in group]
    toxic_tag_counter = Counter(tag_names_in_garbage)
    
    for group in groups:
        # Условие удаления:
        # 1. Группа "популярна" (термин встречается >= 2 раз)
        is_repetitive = len(group) >= 2
        
        # 2. (Опционально) "Зачистка одиночек": 
        # Если термин встретился 1 раз (напр. "ниндзюцу"), но он в теге <strong>, 
        # а тег <strong> в этом тексте удаляется массово (является "токсичным"), то сносим и его.
        is_toxic_tag = False
        if len(group) == 1:
            idx = group[0]
            tag_name = candidates[idx]['tag'].name
            # Если тегов этого типа удалено уже больше 3 штук, считаем тег скомпрометированным
            if toxic_tag_counter[tag_name] > 3:
                is_toxic_tag = True

        if is_repetitive or is_toxic_tag:
            for idx in group:
                tag_obj = candidates[idx]['tag']
                # .unwrap() удаляет сам тег, но оставляет его содержимое на месте
                # Это работает даже если внутри были другие теги или атрибуты
                tag_obj.unwrap()

    # Возвращаем строку (bs4 сам закроет теги если что-то было сломано)
    return str(soup)
    
def finalize_cleanup(html_content: str) -> str:
    """
    Финальная зачистка:
    1. Лечит "сросшиеся" диалоги (Контекстный Митоз или BR).
    2. Превращает выжившие тире в конце абзацев в троеточия.
    3. Расставляет пробелы вокруг троеточий.
    """
    content = html_content
    # --- ЭТАП 0: ЛЕЧЕНИЕ СРОСШИХСЯ ДИАЛОГОВ ---
    # Паттерн: Знак препинания -> Тире -> Эн-даш (или Тире) -> Большая буква
    # Пример: "Привет! — – Хелло!"
    
    def dialogue_mitosis(match):
        punctuation = match.group(1)
        next_char = match.group(2)
        
        # Определяем контекст, глядя назад
        # Ограничиваем поиск, чтобы не сканировать мегабайты текста
        search_limit = 3000 
        search_start = max(0, match.start() - search_limit)
        search_text = content[search_start:match.start()]
        
        # 1. Ищем последнее открытие <p...>
        last_p_open = -1
        last_p_tag = '<p>' # Fallback, если вдруг придется открывать, а атрибуты не найдем
        
        for m in re.finditer(r'(<p(?:\s+[^>]*)?>)', search_text, re.IGNORECASE):
            last_p_open = m.start()
            last_p_tag = m.group(1)
            
        # 2. Ищем последнее закрытие </p>
        last_p_close = search_text.rfind('</p>')
        
        # ЛОГИКА РАЗДЕЛЕНИЯ:
        # Если открытие было ПОЗЖЕ закрытия (или закрытия вообще нет), значит мы ВНУТРИ <p>.
        # Тогда делаем Митоз (закрываем и открываем снова, наследуя атрибуты).
        if last_p_open > last_p_close:
            return f"{punctuation}</p>\n{last_p_tag}— {next_char}"
        
        # Иначе мы в глобальном контейнере (<div>, <body>, <td> и т.д.)
        # Здесь нельзя ставить </p>, поэтому используем <br>
        else:
            return f"{punctuation}<br>\n— {next_char}"
    
    # Ищем артефакт "— –" (Em-Dash + En-Dash), за которым следует Заглавная буква.
    # Строчные буквы (авторские слова "— прокричали") мы НЕ трогаем, это не диалог.
    content = re.sub(
        fr'([.?!…])\s*—\s*[–—]\s*([{UPPERCASE_CHARS}])',
        dialogue_mitosis,
        content
    )
    
    # 1. Нагло меняем конечное тире на троеточие
    content = re.sub(fr'\s*[{DASH_CHARS}]+\s*</p>', f'{ELLIPSIS_CHAR}</p>', content)
    
    # 2. ЗАЧИСТКА ХАОСА

    # --- РАБОТА С ПРОБЕЛАМИ И РЕГИСТРОМ (МАСКИРОВКА) ---
    content, tag_map = _mask_html_tags(content, "TAG_FIN")
    content = ELLIPSIS_CHAOS_PATTERN.sub(ELLIPSIS_CHAR, content)
    
    
    # 3. Тонкая настройка троеточий
    content = re.sub(fr'(?<=[{ALL_LETTER_CHARS}])\s*{ELLIPSIS_CHAR}', ELLIPSIS_CHAR, content)
    content = re.sub(fr'({ELLIPSIS_CHAR})(?=[{ALL_LETTER_CHARS}])', r'\1 ', content)
    content = re.sub(fr'([{DASH_CHARS}])\s*{ELLIPSIS_CHAR}\s+(?=[{ALL_LETTER_CHARS}])', r'\1 …', content)
    content = re.sub(fr'([{DASH_CHARS}])\s*{ELLIPSIS_CHAR}', r'\1 …', content)
    
    # --- ПЕРЕНОС: Работа с регистром теперь под защитой маски ---
    content = re.sub(CAPITALIZE_PATTERN, capitalize_sentence, content)
    content = re.sub(fr'([!?.])(?:{ELLIPSIS_CHAR})', r'\1', content)
    
    # Возвращаем теги
    content = _restore_masked_segments(content, tag_map)
    
    content = clean_glossary_garbage(content)

    # Formatting whitespace is safe to remove only between structural tags.
    # A blanket `>\s+<` replacement also removes visible spaces between inline
    # elements, turning `</em> <strong>` into glued words in rendered HTML.
    struct_tags = r'p|div|h[1-6]|li|blockquote|table|ul|ol|body|html|section|article'
    content = re.sub(
        fr'(</?(?:{struct_tags})\b[^>]*>)\s+(?=</?(?:{struct_tags})\b[^>]*>)',
        r'\1',
        content,
        flags=re.IGNORECASE,
    )

    # Безопасное удаление тире (перед </p>)
    content = re.sub(fr'\s*[{DASH_CHARS}]+\s*</p>', f'{ELLIPSIS_CHAR}</p>', content)
    
    # Список тегов, которые требуют изоляции (блочные)
    # ВАЖНО: Теги вроде <b>, <span>, <a>, <em> СЮДА НЕ ВХОДЯТ, они инлайновые.

    # 2. ИЗОЛЯЦИЯ: Ставим \n ВОКРУГ блоков. Не между, а именно вокруг.
    
    # А. Перед ОТКРЫВАЮЩИМ блочным тегом ставим \n
    # <div> -> \n<div>
    content = re.sub(fr'(?i)(<(?:{struct_tags})\b[^>]*>)', r'\n\1', content)
    
    # Б. После ЗАКРЫВАЮЩЕГО блочного тега ставим \n
    # </div> -> </div>\n
    content = re.sub(fr'(?i)(</(?:{struct_tags})>)', r'\1\n', content)
    
    # В. Спец-обработка для <br>, <hr> и наших КОММЕНТАРИЕВ
    # Они должны быть отбиты с обеих сторон
    content = re.sub(r'(?i)<(br|hr)\s*/?>', r'\n\g<0>\n', content)
    content = re.sub(r'(<!-- SAFE_COMMENT_ID_\d+ -->)', r'\n\1\n', content)

    # 3. КОСМЕТИКА ПЕРЕНОСОВ
    # Если у нас текстовые блоки (p, h1, blockquote), мы хотим ДВОЙНОЙ перенос для красоты.
    # Сейчас у нас везде одинарный (из-за шага 2).
    # Ищем последовательность: Закрытие Текстового -> (пробелы/переносы) -> Открытие Текстового

    text_tags = r'p|h[1-6]|blockquote'
    content = re.sub(
        fr'(</(?:{text_tags})>)\s*(<(?:{text_tags})\b)', 
        r'\1\n\n\2', 
        content, flags=re.IGNORECASE
    )

    # --- ЭТАП 3: ТОЧЕЧНАЯ КОРРЕКЦИЯ (SMART SPLIT) ---
    content = END_COMMA_FIX_PATTERN.sub(ELLIPSIS_CHAR, content)
    content = MISSING_DOT_PATTERN.sub('.', content)

    content, tag_map = _mask_html_tags(content, "TAG_FIN")
    content = ELLIPSIS_CHAOS_PATTERN.sub(ELLIPSIS_CHAR, content)
    
    content = _restore_masked_segments(content, tag_map)

    # Схлопываем тройные переносы, если они где-то возникли
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()

def handle_missing_space(match: re.Match) -> str:
    """
    Добавляет пробел после знаков препинания, если он пропущен.
    Игнорирует точки внутри URL, имен файлов или версий (например, hello.com, v1.0, file.exe),
    если точка окружена латинскими буквами или цифрами.
    """
    sign = match.group(1)
    
    # Если это не точка (запятая, двоеточие и т.д.), всегда добавляем пробел
    if sign != '.':
        return f"{sign} "

    # Для точки проверяем контекст
    pos = match.start()
    text = match.string
    
    # Символ слева от точки
    prev_c = text[pos - 1] if pos > 0 else ''
    # Символ справа от точки (т.к. в паттерне используется lookahead, 
    # match.end() — это позиция сразу за точкой)
    next_c = text[match.end()] if match.end() < len(text) else ''

    # Проверка: является ли символ латиницей или цифрой
    is_prev_tech = ('a' <= prev_c <= 'z') or ('A' <= prev_c <= 'Z') or ('0' <= prev_c <= '9')
    is_next_tech = ('a' <= next_c <= 'z') or ('A' <= next_c <= 'Z') or ('0' <= next_c <= '9')

    # Если с обеих сторон латиница/цифры — возвращаем точку без пробела
    if is_prev_tech and is_next_tech:
        return sign

    return f"{sign} "
    
def capitalize_sentence(match):
    sign = match.group(1)      # Знак (! ? .)
    separator = match.group(2) # То, что между знаком и буквой (пробелы, тире)
    letter = match.group(3)    # Сама буква

    # --- УЛУЧШЕННАЯ ЗАЩИТА ССЫЛОК И ФАЙЛОВ ---
    if sign == '.' and not separator:
        # Получаем символ СЛЕВА от точки
        pos = match.start()
        full_text = match.string
        prev_c = full_text[pos - 1] if pos > 0 else ''
        
        # Проверяем, является ли точка частью технического текста (URL, v1.0, file.exe)
        # Если слева и справа латиница или цифры — не трогаем.
        is_prev_tech = ('a' <= prev_c <= 'z') or ('A' <= prev_c <= 'Z') or ('0' <= prev_c <= '9')
        is_next_tech = ('a' <= letter <= 'z') # letter уже lowercase из паттерна
        
        if is_prev_tech and is_next_tech:
            return match.group(0)

    # 2. ПЕРЕСТРАХОВКА С ПРОБЕЛОМ
    # Если пробела нет (опечатка), добавляем его
    if not separator:
        separator = ' '

    # 3. СБОРКА: Знак + Разделитель + Большая буква
    return f"{sign}{separator}{letter.upper()}"
    
def initial_cleanup(html_content: str) -> str:
    """
    Версия 1.2: Исправлен regex для split_paragraphs.
    Теперь он не захватывает "чужие" закрывающие теги, если внутри встречается
    начало нового параграфа (<p>). Это предотвращает ошибочное схлопывание
    незакрытого параграфа с закрытым соседом.
    """
    if not html_content:
        return ""

    content = html_content
    content = content.replace('\t', '')

    masked_content, tag_map = _mask_html_tags(content, "TAG_INIT")
    masked_content = re.sub(fr'\s+([{COMMA_CHARS}.:;?!])', r'\1', masked_content)
    masked_content = re.sub(fr'([{COMMA_CHARS}:;?!])(?=[{ALL_LETTER_CHARS}])', r'\1 ', masked_content)
    masked_content = re.sub(fr'\.(?=[{UPPERCASE_RU}{LOWERCASE_RU}])', r'. ', masked_content)

    masked_content = _restore_masked_segments(masked_content, tag_map)
    
    content = masked_content

    # Убираем пробелы в начале и в КОНЦЕ контента абзаца
    content = re.sub(r'(<p[^>]*>)\s+', r'\1', content)
    content = re.sub(r'\s+(</p>)', r'\1', content)

    # Превращение переносов внутри P в новые P
    def split_paragraphs(match):
        attrs = match.group(1) 
        inner_text = match.group(2) 
        closing = match.group(3) 
        
        if not re.search(r'(<br\s*/?>|\n)', inner_text, re.IGNORECASE):
            return match.group(0)

        normalized = re.sub(r'<br\s*/?>', '\n', inner_text, flags=re.IGNORECASE)
        normalized = re.sub(r'\n+', '\n', normalized)
        
        parts = normalized.split('\n')
        new_paragraphs = []
        for part in parts:
            p_strip = part.strip()
            if p_strip:
                # Only a real block element may stand on its own. Inline
                # markup such as <em>, <strong>, <span>, or <a> still belongs
                # to the paragraph; dropping the wrapper here leaves visible
                # text directly under <body>/<div> while keeping the raw
                # opening/closing <p> counts deceptively balanced.
                if BLOCK_TAGS_PATTERN.match(p_strip):
                    new_paragraphs.append(p_strip)
                else:
                    new_paragraphs.append(f"{attrs}{p_strip}{closing}")
        
        return "\n".join(new_paragraphs)

    # ВАЖНО: Добавлено условие |<p(?:\s|>) в lookahead.
    # Теперь если мы встретим открывающий <p внутри, совпадение прервется, 
    # и мы не будем пытаться "чинить" битую структуру раньше времени.
    content = re.sub(
        r'(<p[^>]*>)((?:(?!</p>|<p(?:\s|>)).)*?)(</p>)', 
        split_paragraphs, 
        content, 
        flags=re.DOTALL | re.IGNORECASE
    )

    return content



def prettify_html(html_content: str) -> str:
    """
    Версия 8.0 (Attr-Safe): Опасные замены (.. -> …) перенесены в зону маскировки.
    Это защищает пути в атрибутах (src="../") от повреждения.
    """
    if not html_content:
        return ""
    content = html_content
    
    # --- ЭТАП 0: ИЗОЛЯЦИЯ КОММЕНТАРИЕВ ---
    comment_map = {}
    def comment_mask_callback(m):
        key = f'<!-- SAFE_COMMENT_ID_{len(comment_map)} -->'
        comment_map[key] = m.group(0)
        return key
    
    content = re.sub(r'<!--[\s\S]*?-->', comment_mask_callback, content)
    
    # 1. Инициализация и первичная чистка (пробелы внутри абзацев)
    content = optimize_headings(content)
    content = repair_unbalanced_paragraphs(content)
    content = initial_cleanup(content)
    
    
    # --- ЭТАП: ТИПОГРАФИКА И КАВЫЧКИ (МАСКИРОВКА ТЕГОВ) ---
    content, tag_map = _mask_html_tags(content, "TAG")
    
    # Работаем с текстом, пока теги в безопасности
    content = repair_quotes(content)
    content = oper_dash_symbol(content)
    
    # --- ЗОНА БЕЗОПАСНОСТИ (SAFE ZONE) ---
    # Выполняем замены, которые могут сломать атрибуты (src="..", href="file.html"),
    # ПОКА теги все еще скрыты масками.
    content = RAW_AMPERSAND_PATTERN.sub('&amp;', content)
    content = MISSING_SPACE_PATTERN.sub(handle_missing_space, content)
    
    content = ELLIPSIS_CHAOS_PATTERN.sub(ELLIPSIS_CHAR, content) # Здесь .. -> … безопасно для путей
    content = DASH_BEFORE_PUNCTUATION_PATTERN.sub(ELLIPSIS_CHAR, content)
    content = ELLIPSIS_DOT_CLEANUP_PATTERN.sub(ELLIPSIS_CHAR, content)
    content = START_QUOTE_DASH_PATTERN.sub(r'\1…', content)
    
    # ВОЗВРАЩАЕМ ТЕГИ ОБРАТНО
    content = _restore_masked_segments(content, tag_map)

    # --- ЭТАП: СТРУКТУРНАЯ ЧИСТКА (ЗАВИСИТ ОТ ВИДИМОСТИ ТЕГОВ) ---
    # Эти паттерны ищут </p>, поэтому их нельзя запускать под маской
    content = PUNCTUATION_DASH_MERGE_PATTERN.sub(merge_dialogue_punctuation, content)
    content = CAPITALIZATION_FIX_PATTERN.sub(capitalize_next_paragraph_start, content)
    content = PUNCTUATION_CLEANUP_PATTERN.sub(normalize_punctuation, content)
    content = DIALOGUE_SPLIT_PATTERN.sub(dialogue_splitter_with_attributes, content)
    
    # Слияния разорванных строк
    content = REMARK_MERGE_PATTERN.sub(r'\1 \2 ', content)
    content = COMMA_DASH_MERGE_PATTERN.sub(r'\1 \2 ', content)
    content = COMMA_BEFORE_DIALOGUE_MERGE_PATTERN.sub(r'\1 \3 ', content)
    content = LETTER_MERGE_PATTERN.sub(' ', content)
    content = COMMA_LOWERCASE_MERGE_PATTERN.sub(r'\1 ', content)
    
    # Обрывы и троеточия
    content = DASH_TO_ELLIPSIS_PATTERN.sub(ellipsis_replacer, content)
    content = CLEANUP_PATTERN.sub(cleanup_replacer, content)
    content = FINAL_MERGE_PATTERN.sub(rf'{ELLIPSIS_CHAR} \1', content)
    content = SUB_MERGE_PATTERN.sub(r' \1 ', content)
    content = END_DASH_CANDIDATE_PATTERN.sub(smart_end_dash_replacer, content)
    
    # --- ЭТАП: СТРУКТУРНОЕ ФОРМАТИРОВАНИЕ ---
    # 4. Схлопываем лишние пробелы и переносы
    content = re.sub(r'[ \t]+', ' ', content) # Лишние горизонтальные пробелы
    content = re.sub(r'\n\s*\n', '\n', content) # Лишние пустые строки (лесенки)
    
    # --- ЭТАП: ФИНАЛЬНАЯ ТИПОГРАФИКА ---
    content = refine_typography_in_html(content)
    
    content = finalize_cleanup(content)
    
    for key, val in comment_map.items():
        content = content.replace(key, val)
    
    return content.strip()


def get_quote_context(text: str, i: int, quote_chars: set, quote_map: dict) -> str:
    """
    Определяет контекст кавычки, опираясь на Реестр Состояний (quote_map).
    Возвращает: 'STRICT_OPEN', 'STRICT_CLOSE', 'OPEN', 'CLOSE', 'AMBIGUOUS'.
    """
    n = len(text)
    
    # --- СТРУКТУРНАЯ ИНДУКЦИЯ (Поиск по Реестру) ---
    
    # 1. СЛЕВА (Ищем Открывашку)
    l_scan = i - 1
    while l_scan >= 0 and text[l_scan].isspace(): l_scan -= 1
    
    if l_scan >= 0 and l_scan in quote_map:
        rec = quote_map[l_scan]
        # Если сосед - Открывашка и его сила STRICT (Бетон)
        if rec['type'] == 'OPEN' and rec['power'] == 'STRICT':
            return 'STRICT_OPEN' # Мы обязаны стать OPEN, как «

    # 2. СПРАВА (Ищем Закрывашку)
    r_scan = i + 1
    while r_scan < n and text[r_scan].isspace(): r_scan += 1
    
    if r_scan < n and r_scan in quote_map:
        rec = quote_map[r_scan]
        # Если сосед - Закрывашка и его сила STRICT (Бетон)
        if rec['type'] == 'CLOSE' and rec['power'] == 'STRICT':
            return 'STRICT_CLOSE' # Мы обязаны стать CLOSE, как »

    # --- СТАНДАРТНЫЙ АНАЛИЗ (Рентген) ---
    left_idx = i - 1
    while left_idx >= 0 and text[left_idx] in quote_chars:
        left_idx -= 1
    prev_c = text[left_idx] if left_idx >= 0 else ' '
    
    right_idx = i + 1
    while right_idx < n and text[right_idx] in quote_chars:
        right_idx += 1
    next_c = text[right_idx] if right_idx < n else ' '
    
    is_space_left = prev_c.isspace() or prev_c == '\0'
    is_space_right = next_c.isspace() or next_c == '\0'
    
    if next_c in '.,:;?!)]}': return 'CLOSE'
    if prev_c in '([{' or (prev_c in ':;?!' and not is_space_right): return 'OPEN'

    if is_space_left and not is_space_right: return 'OPEN'
    if not is_space_left and is_space_right: return 'CLOSE'
    
    return 'AMBIGUOUS'
    
def repair_quotes(text: str) -> str:
    """
    Универсальный ремонт структуры кавычек.
    Версия: "Absolute Register" (QuoteMap + Strict Skeleton + Flexible Optimizer + Ignore Rescue).
    """
    if not text:
        return ""

    chars = list(text)
    n = len(chars)
    
    # --- РЕЕСТР СОСТОЯНИЙ ---
    # quote_map[index] = { 'type': 'OPEN'/'CLOSE', 'power': 'STRICT'/'FLEXIBLE', 'marry': partner_index }
    quote_map = {} 
    
    # --- ОПРЕДЕЛЕНИЯ ---
    unique_open_chars = set(DETECT_OPEN_QUOTES) - set(DETECT_CLOSE_QUOTES)
    unique_close_chars = set(DETECT_CLOSE_QUOTES) - set(DETECT_OPEN_QUOTES)
    pair_map = dict(zip(QUOTE_HIERARCHY_OPEN, QUOTE_HIERARCHY_CLOSE))
    all_quotes_set = set(ALL_QUOTES)

    quote_indices = [i for i, c in enumerate(chars) if c in ALL_QUOTES]
    
    # 0. Первичная фильтрация (Дюймы -> IGNORE)
    valid_indices = []
    ignore_indices = [] # Кандидаты на воскрешение
    
    for i in quote_indices:
        if chars[i] == '"' and i > 0 and chars[i-1].isdigit():
            # Помечаем как IGNORE, но не добавляем в map пока что
            ignore_indices.append(i) 
        else:
            valid_indices.append(i)

    # =========================================================================
    # ЭТАП 1: СТРОГИЙ КАРКАС (STRICT SKELETON)
    # «...»
    # =========================================================================
    stack = []
    orphan_indices = [] # То, что не вошло в строгий каркас

    for i in valid_indices:
        char = chars[i]
        
        if char in unique_open_chars:
            stack.append(i)
        elif char in unique_close_chars:
            if stack:
                top_idx = stack[-1]
                top_char = chars[top_idx]
                if pair_map.get(top_char) == char:
                    # УСПЕХ: Строгая пара
                    opener = stack.pop()
                    quote_map[opener] = {'type': 'OPEN', 'power': 'STRICT', 'marry': i}
                    quote_map[i] = {'type': 'CLOSE', 'power': 'STRICT', 'marry': opener}
                else:
                    orphan_indices.append(i)
            else:
                orphan_indices.append(i)
        else:
            orphan_indices.append(i)
            
    orphan_indices.extend(stack)
    orphan_indices.sort()

    # =========================================================================
    # ЭТАП 1.5: АСИММЕТРИЧНЫЙ БЕТОН (ASYMMETRIC CONCRETE)
    # „...“ и “...”
    # =========================================================================
    phase2_indices = [i for i in orphan_indices if i not in quote_map]
    processed_in_phase2 = set()

    for idx_in_list, i in enumerate(phase2_indices):
        if i in processed_in_phase2: continue
        
        char = chars[i]
        target_closer = pair_map.get(char)
        
        # Ищем пару, если она существует и она не идентична (не " ищет ")
        if target_closer and target_closer != char:
            
            found_pair_idx = -1
            
            # Lookahead
            for j_list_idx in range(idx_in_list + 1, len(phase2_indices)):
                j = phase2_indices[j_list_idx]
                if j in processed_in_phase2: continue
                
                # ПРОВЕРКА БАРЬЕРА ПО КАРТЕ
                # Проверяем, не перерезаем ли мы существующую пару в map
                is_blocked = False
                
                # Проверка пересечения:
                # Идем по всем кавычкам внутри диапазона (i+1, j)
                # Если кавычка в map (STRICT), проверяем её пару.
                # Если пара снаружи (i, j) -> БЛОК.
                for mid in range(i + 1, j):
                     if mid in quote_map:
                         partner = quote_map[mid]['marry']
                         if partner < i or partner > j: # Партнер снаружи
                             is_blocked = True
                             break
                
                if is_blocked:
                    break 
                
                if chars[j] == target_closer:
                    found_pair_idx = j
                    break
            
            if found_pair_idx != -1:
                quote_map[i] = {'type': 'OPEN', 'power': 'STRICT', 'marry': found_pair_idx}
                quote_map[found_pair_idx] = {'type': 'CLOSE', 'power': 'STRICT', 'marry': i}
                processed_in_phase2.add(found_pair_idx)

    # =========================================================================
    # ЭТАП 2: ПОДГОТОВКА ГИБКОГО ПУЛА
    # =========================================================================
    flexible_pool = []
    
    # Все, что не попало в map и не является уникальным мусором
    remaining = [i for i in orphan_indices if i not in quote_map]
    
    for i in remaining:
        char = chars[i]
        # Строгие уникальные («/») без пары умирают.
        if char in unique_open_chars or char in unique_close_chars:
            quote_map[i] = {'type': 'DELETE', 'power': None, 'marry': None}
        else:
            flexible_pool.append(i)

    # =========================================================================
    # ЭТАП 3: ОПТИМИЗАТОР (THE SOLVER)
    # =========================================================================
    
    def solve_configuration(candidates):
        local_pairs = []
        local_stack = [] 
        
        for idx in candidates:
            # Контекст проверяет quote_map (видит STRICT)
            ctx = get_quote_context(text, idx, all_quotes_set, quote_map)
            
            # --- ИНДУКЦИЯ ПОЛЯРНОСТИ (Уже внутри get_quote_context) ---
            if ctx == 'STRICT_OPEN':
                local_stack.append(idx)
                continue
                
            if ctx == 'STRICT_CLOSE':
                if not local_stack: return -1, [], [] # Fatal
                top_idx = local_stack[-1]
                inner_text = text[top_idx+1 : idx]
                if not inner_text.strip(): return -1, [], [] # Vacuum
                local_stack.pop()
                local_pairs.append((top_idx, idx))
                continue

            # --- МЯГКИЙ КОНТЕКСТ ---
            if not local_stack:
                if ctx != 'CLOSE': local_stack.append(idx)
                continue
                
            top_idx = local_stack[-1]
            top_ctx = get_quote_context(text, top_idx, all_quotes_set, quote_map)
            
            if top_ctx == 'CLOSE' or ctx == 'OPEN':
                local_stack.append(idx)
                continue

            inner_text = text[top_idx+1 : idx]
            if not inner_text.strip():
                local_stack.append(idx)
                continue

            # Проверка Пересечений с STRICT в quote_map
            is_crossed = False
            # Проверяем диапазон (top_idx, idx)
            for mid in range(top_idx + 1, idx):
                if mid in quote_map and quote_map[mid]['power'] == 'STRICT':
                    partner = quote_map[mid]['marry']
                    if partner < top_idx or partner > idx:
                        is_crossed = True; break
            
            if is_crossed:
                local_stack.append(idx)
            else:
                local_stack.pop()
                local_pairs.append((top_idx, idx))
        
        # Скор: количество пар. Штраф за остаток в стеке (не сильно важно, главное макс пар)
        return len(local_pairs), local_pairs, local_stack

    # --- ГЕНЕРАЦИЯ КОНФИГУРАЦИЙ ---
    best_pairs = []
    best_score = -1
    
    configs_to_test = []
    
    # 1. Базовая (все гибкие)
    configs_to_test.append(flexible_pool)
    
    # 2. Удаление одного (если нечетное или просто для пробы)
    if 0 < len(flexible_pool) < 20: # Лимит перебора
        for i in range(len(flexible_pool)):
            subset = flexible_pool[:i] + flexible_pool[i+1:]
            configs_to_test.append(subset)
            
    # 3. ВОСКРЕШЕНИЕ ИГНОРИРУЕМЫХ (Rescue from Ignore)
    # Если мы удалили дюйм, а зря?
    # Пробуем добавить по одному кандидату из ignore_indices
    if len(ignore_indices) > 0 and len(ignore_indices) < 10:
        for ign in ignore_indices:
            # Добавляем в пул, сохраняя сортировку
            new_pool = sorted(flexible_pool + [ign])
            configs_to_test.append(new_pool)

    # ЗАПУСК
    for config in configs_to_test:
        score, pairs, unused = solve_configuration(config)
        if score > best_score:
            best_score = score
            best_pairs = pairs
    
    # ПРИМЕНЕНИЕ
    for s, e in best_pairs:
        quote_map[s] = {'type': 'OPEN', 'power': 'FLEXIBLE', 'marry': e}
        quote_map[e] = {'type': 'CLOSE', 'power': 'FLEXIBLE', 'marry': s}

    # =========================================================================
    # ФИНАЛЬНАЯ СБОРКА
    # =========================================================================
    result = []
    for i, char in enumerate(chars):
        rec = quote_map.get(i)
        
        if rec:
            if rec['type'] == 'DELETE':
                continue
            elif rec['type'] == 'OPEN':
                result.append('«')
            elif rec['type'] == 'CLOSE':
                result.append('»')
            else: # IGNORE (если вдруг попал в map как ignore, хотя мы их фильтровали)
                result.append(char)
        
        elif i in ignore_indices: # Те дюймы, что не пригодились
            result.append(char)
        else:
            # Обычный текст или удаленные кавычки (которым не досталось записи в map)
            if i in quote_indices and i not in ignore_indices:
                # Это кавычка, которой не нашлось места в map (мусор)
                continue 
            result.append(char)
            
    return "".join(result)


def process_markdown_segment(text: str) -> str:
    """
    Ядро алгоритма 'Астроном v6' (Block Barrier).
    
    1. СТРОГИЙ КОНТЕКСТ: Игнорирует звезды, не прилегающие к контенту.
    2. БАРЬЕР БЛОКА: Запрещает соединение звезд, если между ними есть 
       блочный тег (маркированный как \0B_).
    3. ПОТОКИ: Обрабатывает 3, 2 и 1 звезду независимо.
    """
    if '*' not in text:
        return text
    if IS_SEPARATOR_PATTERN.fullmatch(text.strip()):
        return text

    # --- 1. ВАЛИДАТОРЫ КОНТЕКСТА ---
    # \0 — маска тега (любого).
    VALID_OPEN_PREV = set(' ([{"\'«…„\0' + DASH_CHARS)
    VALID_CLOSE_NEXT = set(' )]}"\'»“.,…:;?!\0' + DASH_CHARS)
    NON_CONTENT_CHARS = set(' \t\n\r\0')

    tokens = []
    star_matches = list(re.finditer(r'\*+', text))
    
    if not star_matches:
        return text

    for i, m in enumerate(star_matches):
        start, end = m.span()
        length = len(m.group(0))
        
        char_before = text[start-1] if start > 0 else ' '
        char_after = text[end] if end < len(text) else ' '
        
        # Рентген контекста:
        # OPEN: Слева опора, справа КОНТЕНТ (не пробел, не маска тега).
        can_open = (char_before in VALID_OPEN_PREV) and (char_after not in NON_CONTENT_CHARS)
        # CLOSE: Справа опора, слева КОНТЕНТ.
        can_close = (char_after in VALID_CLOSE_NEXT) and (char_before not in NON_CONTENT_CHARS)
        
        token_state = 'NONE'
        if can_open and can_close: token_state = 'AMBIGUOUS'
        elif can_open: token_state = 'OPEN'
        elif can_close: token_state = 'CLOSE'
        
        if length > 3: token_state = 'NONE'

        tokens.append({'start': start, 'end': end, 'len': length, 'state': token_state})

    replacements = {}
    
    # Обработка потоков (3 -> 2 -> 1)
    for stream_len in [3, 2, 1]:
        stream = [t for t in tokens if t['len'] == stream_len]
        active_opener = None 
        
        for token in stream:
            state = token['state']
            if state == 'NONE': continue
                
            is_closing = (state == 'CLOSE') or (state == 'AMBIGUOUS' and active_opener)
            is_opening = (state == 'OPEN') or (state == 'AMBIGUOUS' and not active_opener)
            
            if is_closing and active_opener:
                # ПРОВЕРКА НА ПЕРЕСКОК БЛОКА (Barrier Check)
                # Ищем специальный маркер \0B_ между потенциальной парой
                inner_region = text[active_opener['end']:token['start']]
                
                if '\0B_' in inner_region:
                    # Найдена стена! Текущий открыватель "сгорает".
                    # Текущий токен может стать новым открывателем, если у него есть право OPEN.
                    active_opener = token if (state == 'OPEN' or state == 'AMBIGUOUS') else None
                else:
                    # Чистый проход. Создаем пару.
                    if stream_len == 3:
                        replacements[active_opener['start']] = ("<strong><em>", 3)
                        replacements[token['start']] = ("</em></strong>", 3)
                    elif stream_len == 2:
                        replacements[active_opener['start']] = ("<strong>", 2)
                        replacements[token['start']] = ("</strong>", 2)
                    elif stream_len == 1:
                        replacements[active_opener['start']] = ("<em>", 1)
                        replacements[token['start']] = ("</em>", 1)
                    active_opener = None # Закрыли туннель
                    
            elif is_opening:
                active_opener = token

    # Сборка
    if not replacements:
        return text
        
    result = []
    last_pos = 0
    for pos in sorted(replacements.keys()):
        replacement_str, source_len = replacements[pos]
        result.append(text[last_pos:pos])
        result.append(replacement_str)
        last_pos = pos + source_len
    result.append(text[last_pos:])
    
    return "".join(result)

def refine_typography_in_html(html_content: str) -> str:
    """
    Умная обработка тире и Markdown.
    ВЕРСИЯ: Double-Pass Masking + Internal Proof Logic.
    ИСПРАВЛЕНИЕ: Корректное определение блочных барьеров при обратном сканировании.
    """
    if not html_content:
        return ""
        
    tag_map = {}

    def mask_callback(m):
        tag_text = m.group(0)
        idx = len(tag_map)
        prefix = "B_" if BLOCK_TAGS_PATTERN.match(tag_text) else ""
        key = f"\0{prefix}TAG_{idx}\0"
        tag_map[key] = tag_text
        return key

    # --- ЭТАП 1: МАСКИРОВКА И MD ---
    # Скрываем исходные теги, прогоняем MD, скрываем теги MD.
    content = re.sub(r'<[^>]+>', mask_callback, html_content)
    content = process_markdown_segment(content)
    content = re.sub(r'<[^>]+>', mask_callback, content)
    content = re.sub(fr'\s*[{DASH_CHARS}]+\s*(?=»)', ELLIPSIS_CHAR, content)
    
    # --- ЭТАП 2: РЕНТГЕН ---
    def get_context_char(text, pos, direction, skip_spaces=False):
        """
        Сканирует текст в заданном направлении, пропуская маскированные теги.
        Если встречает БЛОЧНЫЙ тег (\0B_...), считает его разрывом строки (\n).
        """
        idx = pos
        while 0 <= idx + direction < len(text):
            idx += direction
            char = text[idx]
            
            if char.isspace() and skip_spaces: 
                continue
                
            if char == '\0':
                # Логика работы с масками тегов
                if direction == 1:
                    # ВПЕРЕД: Мы стоим на открывающем \0
                    # Если тег блочный — это стена (\n)
                    if text[idx:idx+3] == '\0B_': return '\n'
                    
                    # Иначе ищем конец маски и прыгаем туда
                    nxt = text.find('\0', idx + 1)
                    if nxt != -1: idx = nxt
                else:
                    # НАЗАД: Мы стоим на закрывающем \0
                    # Нужно найти начало этой маски
                    prv = text.rfind('\0', 0, idx)
                    if prv != -1:
                        # ПРОВЕРКА БАРЬЕРА: Был ли этот тег блочным?
                        if text[prv:prv+3] == '\0B_': return '\n'
                        
                        # Если нет, прыгаем в начало маски. 
                        # (На следующей итерации idx уменьшится и мы выйдем за пределы тега)
                        idx = prv
                continue

            return char
        return '\n'

    # --- ЭТАП 3: ОБРАБОТКА ---
    # Глобальный признак: ИИ использует оператор после запятой хоть раз в документе.

    ai_uses_operators_globally = bool(GLOBAL_OP_PATTERN.search(content))
    
    quote_depth = 0
    dialogue_active = False
    dialogue_started_by_operator = False

    def token_replacer(m):
        nonlocal quote_depth, dialogue_active, dialogue_started_by_operator
        
        # Группа 1: Блочная маска (Граница абзаца)
        if m.group(1):
            dialogue_active = False
            dialogue_started_by_operator = False
            return m.group(1)

        # Группа 2: Кавычки
        if m.group(2):
            char = m.group(2)
            if char == '«':
                quote_depth += 1
                return QUOTE_HIERARCHY_OPEN[(quote_depth - 1) % len(QUOTE_HIERARCHY_OPEN)]
            elif char == '»':
                idx = max(0, quote_depth - 1); quote_depth = max(0, quote_depth - 1)
                return QUOTE_HIERARCHY_CLOSE[idx % len(QUOTE_HIERARCHY_CLOSE)]
            return char

        # Группа 3: Тире
        if m.group(3):
            dash_seq = m.group(3)
            # Рентген теперь корректно видит </p> или </h1> как \n
            prev_sign = get_context_char(content, m.start(), -1, skip_spaces=True)
            
            # А. АКТИВАЦИЯ ДИАЛОГА
            if prev_sign in '\n.!?…:':
                if quote_depth > 0:
                    next_c = get_context_char(content, m.end()-1, 1, skip_spaces=True)
                    if next_c not in f'{ELLIPSIS_CHAR}': 
                        return f'– {ELLIPSIS_CHAR} '
                    else:
                        return f'– '
                
                dialogue_active = True
                if '─' in dash_seq: dialogue_started_by_operator = True
                return '— '

            # Б. СУДЬБА ТИРЕ (после запятой)
            if prev_sign in COMMA_CHARS:
                # Очищаем хвост и всё тело абзаца от масок для анализа
                # Находим границы текущего абзаца (между \n или концами строки)
                start_p = content.rfind('\n', 0, m.start())
                end_p = content.find('\n', m.end())
                p_text = content[start_p if start_p != -1 else 0 : end_p if end_p != -1 else len(content)]
                clean_p = re.sub(r'\0(B_)?TAG_\d+\0', '', p_text)
                
                # resolve_comma_dash смотрит только на первые символы после тире
                # (startswith), поэтому чистим окно, а не весь хвост документа.
                clean_after = re.sub(r'\0(B_)?TAG_\d+\0', '', content[m.end():m.end() + 400]).lstrip()
                
                res = resolve_comma_dash(prev_sign + dash_seq, clean_after, quote_depth, dialogue_active, dialogue_started_by_operator, ai_uses_operators_globally, clean_p)
                return ' ' + res.replace(prev_sign, '', 1).strip() + ' '

            # В. ДЕФИС (внутри слова)
            prev_c = get_context_char(content, m.start(), -1, skip_spaces=False)
            next_c = get_context_char(content, m.end()-1, 1, skip_spaces=False)
            if bool(re.match(fr'[{ALL_LETTER_CHARS}0-9]', prev_c)) and bool(re.match(fr'[{ALL_LETTER_CHARS}0-9]', next_c)):
                if prev_c.isdigit() and next_c.isdigit(): return '–'
                return '-'

            return ' – '

    content = TOKEN_PATTERN.sub(token_replacer, content)
    content = re.sub(r' {2,}', ' ', content)
    content = re.sub(fr'(?<=[{ALL_LETTER_CHARS}0-9])\s*-\s*(?=[{ALL_LETTER_CHARS}0-9])', '-', content)

    content = _restore_masked_segments(content, tag_map)

    return content.strip()
    
    
def resolve_comma_dash(
    comma_seq: str, 
    clean_after: str, 
    depth: int, 
    is_dialogue: bool, 
    is_op_dialogue: bool, 
    has_global_ops: bool,
    clean_body: str
) -> str:
    """
    Решает судьбу тире после запятой (Аудиальный рефлекс).
    
    ВЕРСИЯ: Internal Proof. 
    Начало абзаца с оператора (is_op_dialogue) НЕ является доказательством 
    использования операторной схемы для внутренних тире.
    """
    punct = comma_seq[0] 
    
    # 0. Жесткие границы (Мысли/Это)
    if depth > 0: 
        return f'{punct} –'
    
    # 1. Если это САМ оператор — это безусловный ЗВУК (—)
    if '─' in comma_seq and is_dialogue and is_op_dialogue:
        return f'{punct} —'

    # 2. Жесткие границы (Мысли/Это)
    if clean_after.lower().startswith('это'): 
        return f'{punct} –'
    
    # ПРОВЕРКА: Использует ли ИИ операторы ВНУТРИ текста абзаца (после знаков)
    # Мы ищем доказательство осознанного использования схемы.
    uses_internal_ops = bool(re.search(r'[,!?.…:]\s*─', clean_body))

    # 3. КОНТЕКСТ ДИАЛОГА
    if is_dialogue:
        # Включаем режим "Тишины" (En-dash), только если ИИ доказал, 
        # что умеет в операторы ВНУТРИ текста (здесь или глобально).
        # Начало абзаца (is_op_dialogue) ИГНОРИРУЕТСЯ.
        if is_op_dialogue and (uses_internal_ops or has_global_ops):
            return f'{punct} –'

        # Если ИИ НЕ использует операторную схему внутри текста, 
        # мы доверяем его выбору длины тире.
        if '—' in comma_seq:
            return f'{punct} —'

    # 4. ВНЕ ДИАЛОГА -> Тишина
    return f'{punct} –'


def repair_unbalanced_paragraphs(html_content: str) -> str:
    """
    Эвристическое лечение баланса тегов <p>.
    Версия 5.2: Исправлена ошибка "жадности", когда пробелы оборачивались в <p>.
    Теперь открывающие и закрывающие теги чинятся в одном цикле.
    """
    if not html_content:
        return ""

    content = html_content
    
    # Список блочных контейнеров, которые являются жесткими границами.
    # Добавляем сюда body, div, blockquote, article, section и ячейки таблиц.
    containers = r'body|div|article|section|blockquote|li|td|th'

    # 1. FIX TAIL: <p>Текст...</body> -> <p>Текст...</p></body>
    # Ищем открытый P, который упирается в закрывающий тег контейнера.
    # Lookahead (?=...) проверяет наличие закрывающего тега контейнера сразу после контента.
    content = re.sub(
        fr'(<p(?:\s+[^>]*)?>)((?:(?!</p>).)*?)(?=</(?:{containers})>)',
        r'\1\2</p>',
        content,
        flags=re.IGNORECASE | re.DOTALL
    )

    # 2. FIX HEAD: <body>...Текст...</p> -> <body><p>...Текст...</p>
    # Ищем контент, идущий сразу после открытия контейнера, который заканчивается на </p>.
    # Условие: внутри контента не должно быть открытия <p> (иначе это уже валидная структура).
    # И мы разрешаем внутри любые теги (например <b>), кроме <p>.
    content = re.sub(
        fr'(<(?:{containers})\b[^>]*>)(\s*(?:(?!<p(?:\s|>)).)+?)(</p>)',
        r'\1<p>\2\3',
        content,
        flags=re.IGNORECASE | re.DOTALL
    )
    
    # Прогоняем несколько раз, так как исправление одного тега может 
    # вскрыть проблему в следующем "слое" вложенности.
    for _ in range(5):
        old_content = content
        
        # 1. Сначала чиним пропущенные закрывающие теги </p>
        # Это предотвращает ситуацию <p>Текст<p>Текст</p>
        content = MISSING_CLOSE_PATTERN.sub(r'\1\2</p>', content)
        
        # 2. Затем чиним "сиротский" текст, оборачивая его в <p>
        # Но ТОЛЬКО если это реально текст, а не пробелы/переносы
        match = MISSING_OPEN_PATTERN.search(content)
        if match:
            preceding_tag = match.group(1) 
            orphan_text = match.group(2)
            
            # Ищем стиль последнего абзаца для преемственности
            text_before = content[:match.start()]
            p_matches = list(P_ATTR_SEARCH.finditer(text_before))
            new_opening_tag = "<p>"
            if p_matches:
                last_p = p_matches[-1]
                if last_p.group(1):
                    new_opening_tag = f"<p{last_p.group(1)}>"

            # When the orphan run already ends at </p>, that existing tag is
            # its closing tag. Appending another one used to turn
            # ``</p>text</p>`` into ``</p><p>text</p></p>`` and left exactly
            # the extra closing tags that the final checker reports.
            next_fragment = content[match.end():]
            closing_tag = "" if re.match(r'</p\s*>', next_fragment, re.IGNORECASE) else "</p>"
            replacement = f"{preceding_tag}{new_opening_tag}{orphan_text}{closing_tag}"
            content = content[:match.start()] + replacement + content[match.end():]
        
        # Если за проход ничего не изменилось - выходим
        if content == old_content:
            break

    # Remove closing tags that have no preceding unmatched <p>. They commonly
    # survive at the end of AI output as ``</p></body>`` and cannot be fixed by
    # wrapping text because there is no orphan text between the tags.
    paragraph_depth = 0

    def remove_orphan_closing_tag(match):
        nonlocal paragraph_depth
        raw_tag = match.group(0)
        if raw_tag.lstrip().startswith('</'):
            if paragraph_depth <= 0:
                return ""
            paragraph_depth -= 1
            return raw_tag

        paragraph_depth += 1
        return raw_tag

    content = re.sub(
        r'<p(?:\s+[^>]*)?>|</p\s*>',
        remove_orphan_closing_tag,
        content,
        flags=re.IGNORECASE,
    )

    # Финальный штрих: убираем случайные дубликаты пустых абзацев, если они возникли
    content = re.sub(r'<p[^>]*>\s*</p>', '', content, flags=re.IGNORECASE)
    
    return content

HTML_TAG_TOKEN_RE = re.compile(
    r'</?\s*[A-Za-z][A-Za-z0-9:_-]*'
    r'(?:\s+[A-Za-z_:][A-Za-z0-9:._-]*'
    r'(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'=<>`]+))?)*'
    r'\s*/?>',
    re.DOTALL,
)

DUPLICATED_ANGLE_TAG_PREFIX_RE = re.compile(
    r'<\s*/?\s*(?=</?\s*[A-Za-z][A-Za-z0-9:_-]*(?:\s|/?>))',
    re.IGNORECASE,
)
DUPLICATED_CLOSING_TAG_FRAGMENT_RE = re.compile(
    r'</\s*(?P<tag>p|h[1-6]|li|blockquote|div|section|article)\s*[.,:;/]*\s*'
    r'(?=</\s*(?P=tag)\s*>)',
    re.IGNORECASE,
)
ORPHAN_CLOSING_PREFIX_BEFORE_DASH_RE = re.compile(r'</\s*(?=[—–−─-])')


def _remove_duplicated_angle_tag_prefixes(html_content: str) -> str:
    """Repair broken prefixes such as ``</</p>``, ``</p.</p>`` and ``</—``.

    These are markup artifacts, not visible less-than signs. Escaping the first
    angle bracket would turn the broken prefix into reader-visible ``</`` text.
    """
    if not isinstance(html_content, str) or '<' not in html_content:
        return html_content
    repaired = DUPLICATED_ANGLE_TAG_PREFIX_RE.sub('', html_content)
    repaired = DUPLICATED_CLOSING_TAG_FRAGMENT_RE.sub('', repaired)
    # In dialogue prose an orphan ``</`` before a dash is garbage between two
    # valid sentence parts. Keep a single separator so words do not stick.
    return ORPHAN_CLOSING_PREFIX_BEFORE_DASH_RE.sub(' ', repaired)


def _consume_valid_angle_token(text: str, start: int) -> int | None:
    if text.startswith('<!--', start):
        end = text.find('-->', start + 4)
        return end + 3 if end != -1 else None

    if text.startswith('<![CDATA[', start):
        end = text.find(']]>', start + 9)
        return end + 3 if end != -1 else None

    if text.startswith('<?', start):
        end = text.find('?>', start + 2)
        return end + 2 if end != -1 else None

    doctype_match = re.match(r'<!DOCTYPE\b[^>]*>', text[start:], flags=re.IGNORECASE)
    if doctype_match:
        return start + doctype_match.end()

    tag_match = HTML_TAG_TOKEN_RE.match(text, start)
    if tag_match:
        return tag_match.end()

    return None


def _angle_artifact_preview(html_content: str, index: int, radius: int = 80) -> str:
    start = max(0, index - radius)
    end = min(len(html_content), index + radius + 1)
    raw = html_content[start:end].replace('\r', ' ').replace('\n', ' ')
    try:
        preview = BeautifulSoup(raw, 'html.parser').get_text(" ", strip=True)
    except Exception:
        preview = raw
    preview = re.sub(r'\s+', ' ', preview).strip() or raw.strip()
    if len(preview) > 160:
        preview = preview[:157].rstrip() + "..."
    return preview


def _scan_stray_angle_brackets(html_content: str, collect_limit: int = 0) -> tuple[str, list[str]]:
    if not isinstance(html_content, str) or not html_content:
        return html_content, []

    result = []
    snippets = []
    i = 0
    length = len(html_content)

    while i < length:
        char = html_content[i]

        if char == '<':
            token_end = _consume_valid_angle_token(html_content, i)
            if token_end is not None:
                result.append(html_content[i:token_end])
                i = token_end
                continue

            result.append('&lt;')
            if collect_limit <= 0 or len(snippets) < collect_limit:
                snippets.append(f"<: {_angle_artifact_preview(html_content, i)}")
            i += 1
            continue

        if char == '>':
            result.append('&gt;')
            if collect_limit <= 0 or len(snippets) < collect_limit:
                snippets.append(f">: {_angle_artifact_preview(html_content, i)}")
            i += 1
            continue

        result.append(char)
        i += 1

    return "".join(result), snippets


def escape_stray_angle_brackets(html_content: str) -> str:
    """
    Escapes literal < and > characters that are not part of real HTML/XML tags.
    This keeps visible text intact while making EPUB XHTML parseable again.
    """
    repaired, _ = _scan_stray_angle_brackets(html_content)
    return repaired


def find_stray_angle_bracket_snippets(html_content: str, limit: int = 3) -> list[str]:
    """Return short previews for literal < or > characters outside real tags."""
    _, snippets = _scan_stray_angle_brackets(html_content, collect_limit=limit)
    return snippets[:limit]


def repair_ai_html_artifacts(
    original_html: str,
    translated_html: str,
    protected_terms=None,
) -> str:
    """
    Repairs common AI HTML artifacts without changing translation wording:
    missing body wrapper, duplicated tag prefixes, stray angle brackets,
    unbalanced <p>, and direct visible text inside <body> that should be
    wrapped in paragraphs. It also restores confident missing separators in
    Russian words. A complete translated XHTML shell is preserved byte-for-byte
    around the repaired body.
    """
    if not isinstance(translated_html, str) or not translated_html.strip():
        return translated_html

    original_html = original_html if isinstance(original_html, str) else ""
    preserved_shell = None
    if (
        re.search(r'<body\b', translated_html, re.IGNORECASE)
        and re.search(r'</body>', translated_html, re.IGNORECASE)
    ):
        prefix, _body, suffix = process_body_tag(
            translated_html,
            return_parts=True,
            body_content_only=False,
        )
        if prefix or suffix:
            preserved_shell = (prefix, suffix)

    repaired = normalize_translated_body_wrapper(original_html, translated_html)
    repaired = normalize_xhtml_tag_case(repaired)
    repaired = _remove_duplicated_angle_tag_prefixes(repaired)
    repaired = escape_stray_angle_brackets(repaired)
    repaired = optimize_headings(repaired)
    repaired = repair_unbalanced_paragraphs(repaired)
    repaired = repair_missing_paragraph_tags(original_html, repaired)
    repaired = _coerce_first_heading_level(original_html, repaired)
    repaired = coerce_translated_body_block(original_html, repaired)
    repaired, _glued_word_candidates = repair_glued_russian_words_in_html(
        repaired,
        protected_terms=protected_terms,
    )
    repaired = RAW_AMPERSAND_PATTERN.sub('&amp;', repaired)

    if preserved_shell and re.search(r'<body\b', repaired, re.IGNORECASE):
        repaired_body = process_body_tag(
            repaired,
            return_parts=False,
            body_content_only=False,
        )
        return preserved_shell[0] + repaired_body + preserved_shell[1]
    return repaired


def repair_json_string(json_string: str) -> str | None:
    """
    Восстанавливает поврежденную JSON-строку, извлекая и ремонтируя
    отдельные блоки "ключ":{…}.
    Надежно извлекает все потенциальные блоки, а затем ремонтирует каждый
    в отдельности.
    """
    if not json_string or not isinstance(json_string, str):
        return None

    # --- ЭТАП 1: ИЗВЛЕЧЕНИЕ ---
    # Находим ВСЕ возможные начала блоков "ключ":{…}
    entry_pattern = r'"([^"]+)":\s*{'
    matches = list(re.finditer(entry_pattern, json_string))
    
    if not matches:
        return None

    raw_blocks = []
    for i, current_match in enumerate(matches):
        key = current_match.group(1)
        
        # Начало содержимого нашего объекта (после '{')
        content_start_pos = current_match.end(0)
        
        # Конец нашего блока - это либо начало следующего, либо конец строки
        if i + 1 < len(matches):
            next_match = matches[i+1]
            content_end_pos = next_match.start(0)
        else:
            content_end_pos = len(json_string)
            
        # Вырезаем "сырое" содержимое объекта
        raw_content = json_string[content_start_pos:content_end_pos]
        
        raw_blocks.append({"key": key, "content": raw_content})

    # --- ЭТАП 2: РЕМОНТ ---
    rescued_pairs = []
    for block in raw_blocks:
        key_str = f'"{block["key"]}"'
        content = block["content"]

        # Попытка №1: Может, блок уже почти идеален?
        # Ищем последнюю '}' и проверяем, сбалансированы ли скобки.
        open_braces = 0
        in_string = False
        last_brace_pos = -1
        for i, char in enumerate(content):
            if char == '"' and (i == 0 or content[i-1] != '\\'):
                in_string = not in_string
            elif not in_string:
                if char == '{': open_braces += 1
                elif char == '}': 
                    open_braces -= 1
                    if open_braces == -1: # Нашли закрывающую скобку нашего блока
                        last_brace_pos = i
                        break
        
        if last_brace_pos != -1:
            potential_value = '{' + content[:last_brace_pos+1]
            try:
                json.loads(potential_value)
                rescued_pairs.append((key_str, potential_value))
                continue # Успех! Переходим к следующему блоку.
            except json.JSONDecodeError:
                pass # Не получилось, идем к "тяжелой артиллерии"

        # Попытка №2: "Примерка" скобок (режим Титана)
        repaired = False
        for i in range(1, 6):
            closing_braces = '}' * i
            test_value = '{' + content.rstrip(' \t\n\r,') + closing_braces
            try:
                json.loads(test_value)
                rescued_pairs.append((key_str, test_value))
                repaired = True
                break
            except json.JSONDecodeError:
                continue
        
        # Если не удалось отремонтировать - блок пропускается.

    # --- ЭТАП 3: СБОРКА ---
    if not rescued_pairs:
        return None

    inner_json = ",".join([f"{k}:{v}" for k, v in rescued_pairs])
    final_json = f"{{{inner_json}}}"
    
    return final_json



def split_text_into_chunks(text, target_size, search_window, min_chunk_size):
    """
    Разделяет текст на примерно РАВНОМЕРНЫЕ части, стараясь уважать границы предложений и абзацев.
    Версия 2.0 ("Стратегическая").
    """
    text_len = len(text)
    if text_len <= target_size:
        return [text]

    # 1. Рассчитываем, сколько чанков нам нужно, и какой их идеальный размер.
    num_chunks = math.ceil(text_len / target_size)
    ideal_chunk_size = text_len / num_chunks

    chunks = []
    current_pos = 0

    # 2. Итерируемся N-1 раз, чтобы найти N-1 точку разрыва.
    for i in range(1, num_chunks):
        # 3. Находим идеальную точку для разрыва
        ideal_split_pos = int(ideal_chunk_size * i)
        
        # 4. Определяем "окно поиска" вокруг идеальной точки
        search_start = max(current_pos + min_chunk_size, ideal_split_pos - search_window)
        search_end = min(text_len, ideal_split_pos + search_window)

        # 5. Вызываем нашего "скаута" для поиска лучшего места
        split_pos = _find_best_split_point(text, search_start, search_end)

        # 6. Если "скаут" ничего не нашел или нашел слишком близко, режем по идеальной точке "по-живому"
        if split_pos == -1 or split_pos <= current_pos:
            split_pos = ideal_split_pos
        
        # 7. Отрезаем чанк и обновляем позицию
        chunks.append(text[current_pos:split_pos])
        current_pos = split_pos

    # 8. Добавляем последний "хвост", который является последним чанком
    chunks.append(text[current_pos:])

    return [chunk for chunk in chunks if chunk.strip()]

def _find_best_split_point(text, start_search, end_search):
    """
    Находит наилучшую точку разрыва в HTML-тексте.
    Приоритет: </p> -> <br><br> -> </div>,</h1>.. -> <br> -> .!? -> пробел
    """
    # 1. Ищем закрывающий </p> (самый высокий приоритет)
    pos = text.rfind("</p>", start_search, end_search)
    if pos != -1:
        return pos + len("</p>")

    # 2. Ищем двойной <br>
    # Паттерн для поиска <br><br>, <br/><br/>, <br> <br/> и т.д.
    double_br_match = list(re.finditer(r'(<br\s*/?>\s*){2,}', text[start_search:end_search], re.IGNORECASE))
    if double_br_match:
        return start_search + double_br_match[-1].end()

    # 3. Ищем закрывающие блочные теги
    block_tags_match = list(re.finditer(r'</(div|h[1-6]|blockquote)>', text[start_search:end_search], re.IGNORECASE))
    if block_tags_match:
        return start_search + block_tags_match[-1].end()

    # 4. Ищем одиночный <br>
    single_br_match = list(re.finditer(r'<br\s*/?>', text[start_search:end_search], re.IGNORECASE))
    if single_br_match:
        return start_search + single_br_match[-1].end()

    # 5. Ищем конец предложения (если нет тегов)
    sentence_ends = list(re.finditer(r"[.!?]\s+", text[start_search:end_search]))
    if sentence_ends:
        return start_search + sentence_ends[-1].end()

    # 6. Ищем пробел (самый крайний случай)
    pos = text.rfind(" ", start_search, end_search)
    if pos != -1:
        return pos + 1

    return -1 # Ничего не найдено

def brute_force_split(text):
    """
    Принудительно разделяет текст на две примерно равные части.
    Версия 2.1 ("Унифицированная"): Использует _find_best_split_point для поиска разрыва.
    """
    content_lower = text.lower()
    start_body_tag_pos = content_lower.find('<body')
    end_body_tag_pos = content_lower.rfind('</body>')

    prefix, body_content, suffix = "", text, ""

    if start_body_tag_pos != -1 and end_body_tag_pos != -1:
        start_body_content_pos = content_lower.find('>', start_body_tag_pos) + 1
        prefix = text[:start_body_content_pos]
        body_content = text[start_body_content_pos:end_body_tag_pos]
        suffix = text[end_body_tag_pos:]
    
    text_len = len(body_content)
    mid_point = text_len // 2
    
    # Для очень коротких текстов просто режем пополам
    if text_len < 500:
        split_pos = mid_point
    else:
        # Для длинных текстов ищем умный разрыв в окне вокруг середины
        search_start = max(0, mid_point - int(50 + text_len*0.1))
        search_end = min(text_len, mid_point + int(50 + text_len*0.1))
        
        # *** УПРОЩЕННАЯ ЛОГИКА ***
        split_pos = _find_best_split_point(body_content, search_start, search_end)
        
        # Если умный поиск не удался, режем по середине
        if split_pos == -1: 
            split_pos = mid_point
            
    chunks = [body_content[:split_pos], body_content[split_pos:]]

    return prefix, chunks, suffix


    
def prettify_html_for_ai(html_content: str) -> str:
    """
    ТЕКСТОВЫЙ ХИРУРГ (v8.3 "Deep Mitosis").
    Изменено: 
    1. Regex теперь ищет только "листовые" блоки (без вложенных блоков), 
       чтобы не ломать структуру <div><p>...</p></div>.
    2. Сохранена логика Вкладывания (Nesting) для контейнеров и Клонирования для текста.
    """
    if not html_content:
        return ""

    content = html_content.strip()
    
    # 1. Сначала превращаем явные <br> в переносы
    content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)

    # Список тегов, которые мы считаем структурными
    structural_tags_str = r'p|div|h[1-6]|li|blockquote|table|ul|ol|hr|body|html|section|article|nav|aside|header|footer'
    
    # 2. ПРИНУДИТЕЛЬНАЯ ИЗОЛЯЦИЯ БЛОКОВ
    content = re.sub(
        fr'(?i)(</?(?:{structural_tags_str})\b[^>]*>)', 
        r'\n\1\n', 
        content
    )
    content = re.sub(r'\n{2,}', '\n', content).strip()
    
    BLOCK_TAGS_CHECK = re.compile(fr'^</?(?:{structural_tags_str})\b', re.IGNORECASE)

    # --- PYTHONIC LOGIC ---
    def needs_ellipsis(text_fragment):
        clean = text_fragment.rstrip()
        if not clean: return False
        return clean[-1].isalpha()

    # --- ЭТАП 3: МИТОЗ (Разделение) ---
    def mitosis_callback(m):
        opening = m.group(1)
        tag = m.group(2)
        inner = m.group(3)
        closing = m.group(4)
        
        if '\n' not in inner: return m.group(0)
        
        parts = [p.strip() for p in inner.split('\n') if p.strip()]
        if not parts: return m.group(0)

        # ОПРЕДЕЛЕНИЕ СТРАТЕГИИ
        tag_lower = tag.lower()
        use_nesting = tag_lower in ['div', 'blockquote', 'li', 'section', 'article', 'td', 'th']

        res = []
        for j, p in enumerate(parts):
            current_p = p
            
            # Троеточия (начало)
            if j > 0:
                first_l = re.search(r'[^\W\d_]', current_p) 
                if first_l and first_l.group().islower() and not current_p.startswith('…'):
                    current_p = "…" + current_p
            
            # Троеточия (конец)
            if j < len(parts) - 1:
                if needs_ellipsis(current_p):
                    current_p = current_p + "…"
                    
            if use_nesting:
                # Вкладываем в <p> внутри текущего контейнера
                res.append(f"<p>{current_p}</p>")
            else:
                # Клонируем сам тег (для p, h1...)
                res.append(f"{opening}{current_p}{closing}")
        
        if use_nesting:
            return f"{opening}\n" + "\n".join(res) + f"\n{closing}"
        else:
            return "\n".join(res)

    # --- ГЛАВНОЕ ИЗМЕНЕНИЕ: LEAF NODE REGEX ---
    # Мы ищем теги для митоза: p, div, h1-6, li, blockquote.
    mitosis_tags = r"p|div|h[1-6]|li|blockquote"
    
    # Паттерн работает так:
    # 1. (<({mitosis_tags})...>) -> Открывающий тег (Группа 1, Тег в Группе 2)
    # 2. ( ... ) -> Контент (Группа 3).
    #    Внутри контента используется Lookahead (?!...), который говорит:
    #    "Мэтчи символы до тех пор, пока не встретишь открытие <p, <div и т.д."
    #    ((?:(?!<(?:{mitosis_tags})).)*?)
    # 3. (</\2>) -> Закрывающий тег (Группа 4)
    
    # Это заставляет Regex игнорировать внешние обертки и срабатывать только на 
    # самом глубоком уровне вложенности, где нет других блоков.
    
    content = re.sub(
        fr'(<({mitosis_tags})\b[^>]*>)((?:(?!<(?:{mitosis_tags})).)*?)(</\2>)', 
        mitosis_callback, content, flags=re.DOTALL | re.IGNORECASE
    )

    # --- ЭТАП 4: ОБЕРТКА СИРОТ ---
    lines = [line.strip() for line in content.split('\n') if line.strip()]
    wrapped_lines = []
    total = len(lines)

    for i, line in enumerate(lines):
        is_block = bool(BLOCK_TAGS_CHECK.match(line))
        
        if not is_block:
            if i == 0:
                first_letter = re.search(r'[^\W\d_]', line)
                if first_letter and first_letter.group().islower():
                    line = "…" + line
            
            if i == total - 1:
                if needs_ellipsis(line):
                    line = line + "…"
            
            line = f"<p>{line}</p>"
        
        wrapped_lines.append(line)
    
    content = "\n".join(wrapped_lines)

    # 5. Финальная чистка
    content = COMMA_MERGE_PATTERN.sub(r'\1 ', content)
    content = END_DASH_TO_ELLIPSIS_PATTERN.sub('…</p>', content)
    content = TAG_NEWLINE_PATTERN.sub(r'\1\n', content)
    
    return re.sub(r'\n{2,}', '\n', content).strip()


def unify_paragraphs_for_ai(html_content: str) -> str:
    """
    Приводит HTML к чистому виду (Logic v3.0 - Mitosis):
    1. Вырезает body.
    2. Удаляет мусорные атрибуты у <br>.
    3. Удаляет <br>, висящие в конце блоков.
    4. КЛОНИРОВАНИЕ: Если блок (p, div, h1...) содержит <br>, он разделяется на
       несколько блоков ТОГО ЖЕ ТИПА.
    5. СБОРКА: Голый текст в body оборачивается в <p>, готовые блоки остаются как есть.
    """
    if not html_content or not isinstance(html_content, str):
        return ""

    # 1. ВЫРЕЗАЕМ BODY
    body_regex = r'(<body[^>]*>.*</body>)'
    match = re.search(body_regex, html_content, re.DOTALL | re.IGNORECASE)

    if not match:
        return html_content 

    prefix = html_content[:match.start(0)]
    body_block = match.group(0)
    suffix = html_content[match.end(0):]

    # 2. ПАРСИНГ
    soup = BeautifulSoup(body_block, 'lxml')
    body = soup.body
    if not body:
        return html_content

    # --- ЭТАП 1: ХИРУРГИЧЕСКАЯ ЧИСТКА <BR> ---
    all_brs = body.find_all('br')
    for br in all_brs:
        if br.has_attr('id'): continue # Якоря не трогаем
        br.attrs = {} # Чистим атрибуты

        # Удаление висячих хвостов (trailing BR)
        parent = br.parent
        if parent and parent.name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li', 'blockquote']:
            next_sib = br.next_sibling
            is_trailing = True
            while next_sib:
                if isinstance(next_sib, NavigableString):
                    if next_sib.strip(): 
                        is_trailing = False; break
                elif hasattr(next_sib, 'name'):
                    is_trailing = False; break
                next_sib = next_sib.next_sibling
            if is_trailing:
                br.decompose()

    # --- ЭТАП 1.5 (НОВЫЙ): МИТОЗ КОНТЕЙНЕРОВ ---
    # Ищем блоки, которые нужно разделить
    split_targets = body.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote'])
    
    for container in split_targets:
        # Проверяем, жив ли еще элемент (он мог быть внутри другого уже разделенного контейнера)
        if not container.parent: continue
        
        # Если внутри нет BR, пропускаем
        if not container.find('br'): continue
        
        # --- Логика разделения ---
        # Мы собираем содержимое в группы, разделенные тегами br
        groups = []
        current_group = []
        
        # Важно: работаем с копией списка детей, так как будем их перемещать
        children = list(container.contents)
        
        for child in children:
            if hasattr(child, 'name') and child.name == 'br':
                # BR - это разделитель. Завершаем текущую группу.
                # (Сам BR просто исчезает, так как он превращается в разрыв между блоками)
                groups.append(current_group)
                current_group = []
            else:
                current_group.append(child)
        groups.append(current_group) # Добавляем хвост
        
        # Теперь создаем клоны
        for group in groups:
            # Проверяем, не пустая ли группа (игнорируем пробелы)
            is_empty = True
            for node in group:
                if isinstance(node, NavigableString):
                    if node.strip(): 
                        is_empty = False; break
                else:
                    # Любой тег (img, span, b) считается контентом
                    is_empty = False; break
            
            if is_empty: continue
            
            # Создаем новый тег того же типа с теми же атрибутами
            new_block = soup.new_tag(container.name, **container.attrs)
            
            # Переносим содержимое
            for node in group:
                new_block.append(node)
            
            # Вставляем перед оригинальным контейнером
            container.insert_before(new_block)
            
        # Удаляем исходный контейнер (он теперь пуст или его содержимое распределено)
        container.decompose()

    # --- ЭТАП 2: ФИНАЛЬНАЯ СБОРКА BODY ---
    # Теперь у нас в body лежат либо блочные теги (старые или новые клоны), либо мусор.
    # Текст, который лежал прямо в body (без обертки), нужно обернуть в p.
    
    def is_separator(element):
        # Блочные элементы - это "готовые" части, их не надо трогать
        if hasattr(element, 'name') and element.name in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'blockquote', 'hr', 'table', 'ul', 'ol']:
            return True
        # BR прямо в body - это разделитель для голого текста
        if hasattr(element, 'name') and element.name == 'br':
            return True
        # Пустой текст - разделитель
        if isinstance(element, NavigableString) and not element.strip():
            return True
        return False

    new_body_content = []
    
    # Группируем содержимое body
    # not is_sep = True -> это группа голого текста/инлайнов, которую надо обернуть в <p>
    # not is_sep = False -> это разделитель или готовый блок
    for is_sep, group in groupby(body.contents, key=is_separator):
        if not is_sep:
            # Группа голого контента -> Оборачиваем в <p>
            new_p = soup.new_tag('p')
            has_content = False
            for node in group:
                if isinstance(node, NavigableString):
                    cleaned_text = re.sub(r'\s+', ' ', str(node)).strip()
                    if cleaned_text:
                        new_p.append(NavigableString(cleaned_text))
                        has_content = True
                else:
                    new_p.append(node)
                    has_content = True
            
            if has_content:
                new_body_content.append(new_p)
        else:
            # Группа разделителей/блоков
            for element in group:
                # Блочные теги (p, div...) переносим как есть
                if hasattr(element, 'name') and element.name != 'br':
                    new_body_content.append(element)
                # <br> в корне body просто игнорируем, так как они уже разделили группы текста

    body.clear()
    body.extend(new_body_content)

    processed_body_str = str(body)
    # Косметика
    formatted_body = re.sub(r'(</(p|h[1-6]|div)>)', r'\1\n', processed_body_str)

    return prefix + formatted_body + suffix


def shouldUseWordBoundaries(
    text: str,
    cjk_dominance_threshold: float = 0.5,
    space_density_anomaly_threshold: float = 0.04
) -> bool:
    """
    Определяет, следует ли использовать границы слов (\b) при поиске в большом
    куске литературного текста.

    Это мощная эвристика, которая анализирует состав символов и плотность пробелов.

    Логика:
    1.  Сначала определяется доминирующий тип письма. Если более 50% символов
        относятся к "слитным" CJK-скриптам (китайский, японский), то границы
        слов (\b) точно не нужны. Возвращается `False`.
    2.  Если текст преимущественно "пробельный" (латиница, кириллица, корейский),
        проверяется фактическая плотность пробелов. Если она аномально низкая
        (ниже 4%, что типично для текста без пробелов), это считается ошибкой
        форматирования, и \b будет бесполезен. Возвращается `False`.
    3.  Во всех остальных случаях (т.е. это "пробельный" текст с нормальным
        количеством пробелов) возвращается `True`.

    Args:
        text (str): Входной текст для анализа (обычно большой фрагмент).
        cjk_dominance_threshold (float): Порог, при котором CJK-письмо
                                         считается доминирующим.
        space_density_anomaly_threshold (float): Порог плотности пробелов, ниже
                                                 которого текст считается "слитным"
                                                 даже для пробельных языков.

    Returns:
        bool: True, если поиск с границами слов (\b) является предпочтительной стратегией.
              False, если следует использовать простой поиск подстроки.
    """
    text_len = len(text)
    if text_len < 25:
        # Для коротких строк эвристика ненадежна, безопаснее использовать \b
        return True

    # --- Шаг 1: Анализ состава символов ---
    cjk_unspaced_count = len(CJK_UNSPACED_RE.findall(text))
    
    # Главное правило: если текст преимущественно китайский/японский, \b не нужен.
    cjk_ratio = cjk_unspaced_count / text_len
    if cjk_ratio >= cjk_dominance_threshold:
        return False

    # Если CJK-символов мало, проверяем наличие "пробельных" символов.
    # Это нужно, чтобы отсечь текст, состоящий только из цифр и знаков препинания.
    spaced_scripts_count = len(SPACED_SCRIPTS_RE.findall(text))
    if spaced_scripts_count == 0:
        # Если в тексте нет букв, требующих пробелов, \b не имеет смысла.
        return False

    # --- Шаг 2: Анализ плотности пробелов для "пробельных" текстов ---
    
    # Если мы дошли сюда, текст считается "пробельным" по своему составу.
    # Теперь нужно проверить, не является ли он аномальным по своей структуре.
    # sum() с генератором часто быстрее, чем re.findall() для одного символа.
    space_count = sum(1 for char in text if char.isspace())
    space_density = space_count / text_len

    # Если плотность пробелов аномально низкая, значит, \b не сработает.
    if space_density < space_density_anomaly_threshold:
        return False

    # Если это "пробельный" текст с нормальной плотностью пробелов - используем \b.
    return True

def replace_terms_in_html(html_content: str, replacements: dict) -> str:
    """
    Заменяет термины в HTML-коде на основе словаря, корректно обрабатывая
    вложенные теги и разные типы языков.
    Версия 7.1: Исправлена логика и восстановлен надежный regex.
    """
    if not replacements or not html_content:
        return html_content

    # 1. Изолируем <body>
    body_regex = re.compile(r'(<body[^>]*>.*</body>)', re.DOTALL | re.IGNORECASE)
    match = body_regex.search(html_content)

    if match:
        prefix = html_content[:match.start(1)]
        body_content = match.group(1)
        suffix = html_content[match.end(1):]
    else:
        prefix, suffix = "", ""
        body_content = html_content

    # 2. Определяем стратегию поиска ОДИН РАЗ
    try:
        clean_text_for_heuristic = BeautifulSoup(body_content, 'html.parser').get_text()
    except Exception:
        clean_text_for_heuristic = re.sub('<[^<]+?>', '', body_content)
    
    # --- ИСПРАВЛЕНИЕ №1: Правильное использование эвристики ---
    use_boundaries = shouldUseWordBoundaries(clean_text_for_heuristic)
    boundary = r'\b' if use_boundaries else ''
    
    # 3. Сортируем ключи от длинных к коротким - это ключ к успеху
    sorted_originals = sorted(replacements.keys(), key=len, reverse=True)
    
    # 4. Последовательно заменяем термины
    processed_body = body_content
    for original in sorted_originals:
        rus = replacements[original]
        try:
            # --- ИСПРАВЛЕНИЕ №2: Мощный regex, который "перепрыгивает" теги ---
            # Экранируем исходный термин и разбиваем его на слова (для фраз)
            words = re.escape(original).split(r'\ ')
            # Собираем паттерн, который позволяет иметь сколько угодно тегов и пробелов между словами
            pattern_core = r'(\s*<[^>]+>\s*)*'.join(words)
            # Добавляем границы слов, если это необходимо
            pattern_str = boundary + pattern_core + boundary
            
            pattern = re.compile(pattern_str, re.IGNORECASE)

            replacement_html = create_glossary_span(original, rus)
            
            # re.sub() сам найдет и заменит все непересекающиеся вхождения
            processed_body = pattern.sub(replacement_html, processed_body)
            
        except re.error as e:
            # Защита от некорректных терминов, которые могут сломать regex
            print(f"Ошибка при компиляции regex для термина '{original}': {e}")
            continue
            
    # 5. Форматируем и собираем обратно (если нужно)
    # В данном случае, дополнительное форматирование может быть излишним, 
    # так как мы уже работали с HTML
    return prefix + processed_body + suffix
    


def create_glossary_span(original: str, rus: str) -> str:
    """
    Создает безопасную HTML-строку для термина из глоссария.

    Автоматически экранирует спецсимволы в атрибутах и содержимом.
    """
    # Экранируем, чтобы избежать проблем с кавычками и тегами
    safe_original = html.escape(original, quote=True)
    safe_translation = html.escape(rus)
    
    return f'<span class="glossary-term" title="{safe_original}">{safe_translation}</span>'
   
   
# Паттерн находит:
# Группа 1: Экранированные скобки {{ или }}
# Группа 2: Валидный плейсхолдер {ключ} (захватывая ключ в группу 3)
# Группа 4: Одиночные { или }
FORMAT_PATTERN = re.compile(r"(\{\{|\}\})|(\{([a-zA-Z_][a-zA-Z0-9_]*)\})|([\{\}])")
    
    
def safe_format(template_string: str, **kwargs) -> str:
    """
    Безопасно форматирует строку.
    Версия 5.1 (Suppression Tags): Распознает теги <suppress_..._injection/>,
    которые отключают авто-добавление для конкретного ключа.
    """
    if not template_string:
        return ""

    result_string = template_string
    
    for key, value in kwargs.items():
        placeholder = f"{{{key}}}"
        # Создаем тег-глушилку для текущего ключа
        suppress_tag = f"<suppress_{key}_injection/>"

        # --- НОВАЯ ЛОГИКА: ПРОВЕРКА НА ПОДАВЛЕНИЕ ---
        if suppress_tag in result_string:
            # 1. Удаляем тег-глушилку из строки.
            result_string = result_string.replace(suppress_tag, "")
            # 2. Пропускаем любую дальнейшую обработку для этого ключа.
            continue
        # --- КОНЕЦ НОВОЙ ЛОГИКИ ---
        
        # Гарантируем строку. None превращаем в пустую строку.
        val_str = str(value) if value is not None else ""
        
        if placeholder in result_string:
            # СЦЕНАРИЙ 1: Плейсхолдер найден. Штатная замена.
            result_string = result_string.replace(placeholder, val_str)
        
        elif val_str:
            # СЦЕНАРИЙ 2: Плейсхолдера нет, но данные есть.
            # Дописываем в конец ("Protection Injection").
            
            # Добавляем отбивку, если строка не кончается переводом строки
            prefix = "\n" if not result_string.endswith("\n") else ""
            
            # Формируем XML-блок
            injection = f"{prefix}\n<{key}>\n{val_str}\n</{key}>\n"
            
            result_string += injection
            
    return result_string


def process_body_tag(full_html_content: str, return_parts: bool = False, body_content_only: bool = True):
    """
    Универсальный "хирург" для работы с тегом <body>. Управляется двумя флагами.

    :param full_html_content: Полный HTML-код для обработки.
    :param return_parts:
        - False (по умолчанию): Возвращает одну строку - контент.
        - True: Возвращает кортеж из трех частей (prefix, content, suffix).
    :param body_content_only:
        - True (по умолчанию): В "контентной" части будут только внутренности <body>.
        - False: В "контентную" часть будут включены и сами теги <body>...</body>.

    Сценарии:
    1. return_parts=False, body_content_only=True (по умолчанию):
       -> 'внутреннее содержимое body'
       (Идеально для очистки ответа AI)

    2. return_parts=False, body_content_only=False:
       -> '<body>...внутреннее содержимое...</body>'
       (Получить только сам блок body с тегами)

    3. return_parts=True, body_content_only=False:
       -> ('префикс', '<body>...</body>', 'суффикс')
       (Разделить документ на три части, сохраняя целостность body)

    4. return_parts=True, body_content_only=True:
       -> ('префикс<body>', 'внутренности', '</body>суффикс')
       (Разделить документ так, чтобы теги body "обнимали" префикс и суффикс)
    """
    if not isinstance(full_html_content, str):
        content = str(full_html_content)
        return ("", content, "") if return_parts else content

    # --- Поиск ключевых позиций ---
    content_lower = full_html_content.lower()
    start_body_tag_pos = content_lower.find('<body')
    end_body_tag_pos = content_lower.rfind('</body>', start_body_tag_pos)

    # --- Сценарий 1: Тег <body> не найден ---
    if start_body_tag_pos == -1 or end_body_tag_pos == -1:
        return ("", full_html_content, "") if return_parts else full_html_content

    # --- Сценарий 2: Тег <body> найден, извлекаем все части ---
    start_content_pos = full_html_content.find('>', start_body_tag_pos) + 1
    end_slice_pos = end_body_tag_pos + len('</body>')

    prefix = full_html_content[:start_body_tag_pos]
    open_body_tag = full_html_content[start_body_tag_pos:start_content_pos]
    inner_content = full_html_content[start_content_pos:end_body_tag_pos]
    close_body_tag = full_html_content[end_body_tag_pos:end_slice_pos]
    suffix = full_html_content[end_slice_pos:]
    
    body_with_tags = open_body_tag + inner_content + close_body_tag

    # --- Возвращаем результат в зависимости от флагов ---
    if return_parts:
        if body_content_only:
            return (prefix + open_body_tag, inner_content.strip(), close_body_tag + suffix)
        else:
            return (prefix, body_with_tags, suffix)
    else:
        if body_content_only:
            return inner_content.strip()
        else:
            return body_with_tags


def normalize_translated_body_wrapper(original_html: str, translated_html: str) -> str:
    """
    Restores the original <body ...>...</body> wrapper when the model returns
    only body inner HTML or only one side of the wrapper.
    """
    if not isinstance(translated_html, str) or not translated_html.strip():
        return translated_html
    if not isinstance(original_html, str):
        return translated_html

    original_lower = original_html.lower()
    if '<body' not in original_lower or '</body>' not in original_lower:
        return translated_html

    translated_lower = translated_html.lower()
    has_body_start = bool(re.search(r'<body\b', translated_lower))
    has_body_end = bool(re.search(r'</body>', translated_lower))
    if has_body_start and has_body_end:
        return translated_html

    open_body_match = re.search(r'<body\b[^>]*>', original_html, re.IGNORECASE)
    open_body_tag = open_body_match.group(0) if open_body_match else "<body>"

    inner_html = _strip_document_shell_for_body_repair(translated_html)

    return f"{open_body_tag}{inner_html}</body>"


def _strip_document_shell_for_body_repair(html_content: str) -> str:
    """Remove XHTML document chrome when the model returned content outside body."""
    fragment = (html_content or "").strip()

    body_match = re.search(r'<body\b[^>]*>', fragment, re.IGNORECASE)
    if body_match:
        fragment = fragment[body_match.end():]
    else:
        fragment = re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', fragment, flags=re.IGNORECASE)
        fragment = re.sub(r'^\s*<!DOCTYPE[\s\S]*?>\s*', '', fragment, flags=re.IGNORECASE)

        html_match = re.search(r'<html\b[^>]*>', fragment, re.IGNORECASE)
        if html_match:
            fragment = fragment[html_match.end():]

        fragment = re.sub(r'^\s*<head\b[\s\S]*?</head>\s*', '', fragment, flags=re.IGNORECASE)
        stray_head_end = re.search(r'</head\s*>\s*', fragment, re.IGNORECASE)
        if stray_head_end:
            before_head_end = fragment[:stray_head_end.start()]
            has_body_content_before_head = re.search(
                r'<(?:h[1-6]|p|div|section|article|blockquote|ul|ol|table|figure)\b',
                before_head_end,
                re.IGNORECASE,
            )
            if not has_body_content_before_head:
                fragment = fragment[stray_head_end.end():]

    for _ in range(2):
        fragment = re.sub(r'\s*</(?:body|html)>\s*$', '', fragment, flags=re.IGNORECASE)

    fragment = re.sub(r'^\s*<body\b[^>]*>\s*', '', fragment, flags=re.IGNORECASE)
    fragment = re.sub(r'\s*</body>\s*$', '', fragment, flags=re.IGNORECASE)
    return fragment.strip()


def coerce_translated_body_block(original_html: str, translated_html: str) -> str:
    """
    Return only the translated <body>...</body> block when the source had one.
    This is a final save guard for model responses that leak full XHTML shells
    or omit one/both body tags.
    """
    normalized = normalize_translated_body_wrapper(original_html, translated_html)
    if not isinstance(normalized, str) or not isinstance(original_html, str):
        return normalized

    soup_cache = {}
    normalized_original = _normalize_epub_heading_for_validation(original_html, soup_cache)
    normalized = _normalize_epub_heading_for_validation(normalized, soup_cache)
    normalized = _coerce_first_heading_level(normalized_original, normalized, soup_cache)

    original_lower = normalized_original.lower()
    if '<body' not in original_lower or '</body>' not in original_lower:
        return normalized

    normalized_lower = normalized.lower()
    if re.search(r'<body\b', normalized_lower) and re.search(r'</body>', normalized_lower):
        normalized = process_body_tag(normalized, return_parts=False, body_content_only=False)
    return repair_missing_paragraph_tags(normalized_original, normalized, soup_cache)


def _parse_html_for_validation(html_content, soup_cache=None):
    """Парсит HTML, переиспользуя soup одной и той же строки в рамках вызова.

    Кэшированные soup — read-only: хелпер, которому нужно мутировать дерево,
    обязан парсить свежую копию либо удалить ключ из кэша до мутации.
    """
    if soup_cache is None:
        return BeautifulSoup(html_content, 'html.parser')
    soup = soup_cache.get(html_content)
    if soup is None:
        soup = BeautifulSoup(html_content, 'html.parser')
        soup_cache[html_content] = soup
    return soup


def _normalize_epub_heading_for_validation(html_content: str, soup_cache=None) -> str:
    try:
        from .epub_tools import normalize_epub_chapter_heading_to_h1
    except Exception:
        return html_content
    return normalize_epub_chapter_heading_to_h1(html_content, soup_cache=soup_cache)


def _first_heading_tag(soup):
    root = soup.body if getattr(soup, "body", None) else soup
    return root.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) if root else None


def _coerce_first_heading_level(original_html: str, translated_html: str, soup_cache=None) -> str:
    if not isinstance(original_html, str) or not isinstance(translated_html, str):
        return translated_html

    original_soup = _parse_html_for_validation(original_html, soup_cache)
    translated_soup = _parse_html_for_validation(translated_html, soup_cache)
    original_heading = _first_heading_tag(original_soup)
    translated_heading = _first_heading_tag(translated_soup)
    if not original_heading or not translated_heading:
        return translated_html

    original_name = str(original_heading.name or "").lower()
    translated_name = str(translated_heading.name or "").lower()
    if original_name == translated_name:
        return translated_html
    if original_name != 'h1' or translated_name not in {'h2', 'h3', 'h4', 'h5', 'h6'}:
        return translated_html
    if translated_soup.find('h1') is not None:
        return translated_html

    if soup_cache is not None:
        # Дерево сейчас мутирует и перестанет соответствовать translated_html.
        soup_cache.pop(translated_html, None)
    translated_heading.name = original_name
    return str(translated_soup)


def _paragraph_tag_count(html_content: str) -> int:
    if not isinstance(html_content, str):
        return 0
    return len(re.findall(r'<p(?:\s|>)', html_content, flags=re.IGNORECASE))


def _large_translation_min_ratio(source_text: str) -> float:
    if CJK_UNSPACED_RE.search(source_text or ""):
        return 0.70
    return 0.45


def find_unwrapped_body_text_snippets(html_content: str, limit: int = 3) -> list[str]:
    """Return visible direct text/inline content in <body> that is not inside a block tag."""
    if not isinstance(html_content, str) or not html_content.strip():
        return []

    soup = BeautifulSoup(html_content, 'html.parser')
    body = soup.body
    if not body:
        return []

    snippets = []
    ignored_nodes = (Comment, Declaration, ProcessingInstruction)
    for child in body.contents:
        if isinstance(child, ignored_nodes):
            continue

        text = ""
        if isinstance(child, NavigableString):
            text = str(child)
        elif getattr(child, 'name', None) in INLINE_BODY_START_TAGS:
            text = child.get_text(" ", strip=True)

        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            snippets.append(text[:120])
            if len(snippets) >= limit:
                break

    return snippets


def _split_root_text_paragraphs(text: str) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []

    if "\n" in normalized:
        parts = [part.strip() for part in re.split(r'\n\s*\n+', normalized) if part.strip()]
        if len(parts) == 1:
            line_parts = [line.strip() for line in normalized.split("\n") if line.strip()]
            if len(line_parts) > 1:
                return line_parts
        return parts

    return [normalized.strip()]


def _body_has_wrappable_root_content(soup) -> bool:
    """Read-only проверка условий, при которых _wrap_body_root_text_as_paragraphs
    что-то меняет: голый текст, <br> или inline-тег с текстом в корне <body>."""
    body = soup.body
    if not body:
        return False

    ignored_nodes = (Comment, Declaration, ProcessingInstruction)
    for child in body.contents:
        if isinstance(child, ignored_nodes):
            continue
        if isinstance(child, NavigableString):
            if str(child).strip():
                return True
            continue
        child_name = getattr(child, 'name', None)
        if child_name == 'br':
            return True
        if child_name in INLINE_BODY_START_TAGS and child.get_text(" ", strip=True):
            return True
    return False


def _wrap_body_root_text_as_paragraphs(html_content: str, soup_cache=None) -> tuple[str, bool]:
    probe_soup = _parse_html_for_validation(html_content, soup_cache)
    if not _body_has_wrappable_root_content(probe_soup):
        return html_content, False

    # Мутирующий проход: всегда свежий парс, кэшированный soup не трогаем.
    soup = BeautifulSoup(html_content, 'html.parser')
    body = soup.body
    if not body:
        return html_content, False

    ignored_nodes = (Comment, Declaration, ProcessingInstruction)
    new_children = []
    buffer = []
    changed = False

    def flush_buffer():
        nonlocal buffer, changed
        if not buffer:
            return
        paragraph = soup.new_tag('p')
        for item in buffer:
            paragraph.append(item)
        new_children.append(paragraph)
        buffer = []
        changed = True

    for child in list(body.contents):
        if isinstance(child, ignored_nodes):
            flush_buffer()
            new_children.append(child.extract())
            continue

        if isinstance(child, NavigableString):
            parts = _split_root_text_paragraphs(str(child))
            child.extract()
            if not parts:
                continue
            for index, part in enumerate(parts):
                if buffer:
                    buffer.append(NavigableString(" "))
                buffer.append(NavigableString(part))
                if len(parts) > 1 or index < len(parts) - 1:
                    flush_buffer()
            changed = True
            continue

        child_name = getattr(child, 'name', None)
        if child_name == 'br':
            child.extract()
            flush_buffer()
            changed = True
            continue

        if child_name in INLINE_BODY_START_TAGS and child.get_text(" ", strip=True):
            if buffer:
                buffer.append(NavigableString(" "))
            buffer.append(child.extract())
            changed = True
            continue

        flush_buffer()
        new_children.append(child.extract())

    flush_buffer()

    if not changed:
        return html_content, False

    body.clear()
    body.extend(new_children)
    return str(soup), True


def repair_missing_paragraph_tags(original_html: str, translated_html: str, soup_cache=None) -> str:
    """
    Wrap translated root-level body text into <p> tags when the source used
    paragraphs and the model flattened them into plain text.
    """
    if not isinstance(original_html, str) or not isinstance(translated_html, str):
        return translated_html

    original_p_count = _paragraph_tag_count(original_html)
    if original_p_count <= 0:
        return translated_html

    original_soup = _parse_html_for_validation(original_html, soup_cache)
    translated_soup = _parse_html_for_validation(translated_html, soup_cache)
    if (
        not _find_leading_visible_text_before_expected_block(original_soup)
        and _find_leading_visible_text_before_expected_block(translated_soup)
    ):
        translated_html, _ = _repair_leading_visible_text_before_expected_block(
            original_soup,
            translated_html,
        )

    repaired_html, repaired = _wrap_body_root_text_as_paragraphs(translated_html, soup_cache)
    if not repaired:
        return translated_html

    return repaired_html


def _create_structural_fingerprint(soup):
        """Создает 'отпечаток' HTML-структуры для быстрого сравнения."""
        fp = {
            'headings': {}, 
            'images': len(soup.find_all('img')), 
            'links': len(soup.find_all('a')), 
            'lists': len(soup.find_all(['ol', 'ul']))
        }
        for h_tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            fp['headings'][h_tag.name] = fp['headings'].get(h_tag.name, 0) + 1
        return fp

EXPECTED_BODY_START_BLOCKS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div'}
INLINE_BODY_START_TAGS = {
    'a', 'abbr', 'b', 'bdi', 'bdo', 'cite', 'code', 'data', 'dfn', 'em',
    'i', 'kbd', 'mark', 'q', 'rp', 'rt', 'ruby', 's', 'small', 'span',
    'strong', 'sub', 'sup', 'time', 'u', 'var',
}


def _find_leading_visible_text_before_expected_block(soup):
    """Finds stray visible text before the first expected content block in <body>."""
    body = soup.body
    if not body:
        return ""

    ignored_nodes = (Comment, Declaration, ProcessingInstruction)

    for node in body.descendants:
        if node is body or isinstance(node, ignored_nodes):
            continue

        if getattr(node, 'name', None) in EXPECTED_BODY_START_BLOCKS:
            return ""

        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                return re.sub(r'\s+', ' ', text)[:120]

    return ""


def _first_expected_body_block_name(soup):
    body = soup.body
    if not body:
        return "p"

    ignored_nodes = (Comment, Declaration, ProcessingInstruction)
    for node in body.descendants:
        if node is body or isinstance(node, ignored_nodes):
            continue
        name = getattr(node, 'name', None)
        if name in EXPECTED_BODY_START_BLOCKS:
            return name
    return "p"


def _repair_leading_visible_text_before_expected_block(original_soup, translated_html):
    """
    Wraps a leading root-level text run in the same first block type used by
    the source document. This repairs model responses like:
    <body>Chapter title<p>...</p></body>
    """
    soup = BeautifulSoup(translated_html, 'html.parser')
    body = soup.body
    if not body:
        return translated_html, False

    wrapper_name = _first_expected_body_block_name(original_soup)
    wrapper = soup.new_tag(wrapper_name)
    insert_before = None
    moved_any = False
    ignored_nodes = (Comment, Declaration, ProcessingInstruction)

    for child in list(body.children):
        if isinstance(child, ignored_nodes):
            continue

        if isinstance(child, NavigableString):
            text = str(child)
            if not text.strip():
                if moved_any:
                    child.extract()
                continue
            if wrapper.contents:
                wrapper.append(NavigableString(" "))
            wrapper.append(NavigableString(text.strip()))
            child.extract()
            moved_any = True
            continue

        child_name = getattr(child, 'name', None)
        if child_name in EXPECTED_BODY_START_BLOCKS:
            insert_before = child
            break

        if child_name == 'br':
            if moved_any:
                child.extract()
            continue

        if child_name in INLINE_BODY_START_TAGS and child.get_text(" ", strip=True):
            if wrapper.contents:
                wrapper.append(NavigableString(" "))
            wrapper.append(child.extract())
            moved_any = True
            continue

        insert_before = child
        break

    if not moved_any:
        return translated_html, False

    if insert_before is not None and getattr(insert_before, 'parent', None) is body:
        insert_before.insert_before(wrapper)
    else:
        body.append(wrapper)

    return str(soup), True


def optimize_headings(html_content: str) -> str:
    """
    Принудительно закрывает заголовки (h1-h6), если они содержат блочные элементы.
    Удаляет пробелы перед закрытием и вычищает ВСЕ сиротские закрывающие теги.
    """
    if not html_content:
        return ""
    
    content = html_content
    
    # 1. FORCE CLOSE LOGIC (Закрытие перед блоками)
    # Ищем H-тег, который не закрывается до начала блочного элемента.
    block_start_pattern = r'(?=<p(?:\s|>)|<div(?:\s|>)|<blockquote|<ul(?:\s|>)|<ol(?:\s|>)|<li(?:\s|>)|<table)'
    
    def close_h_callback(match):
        full_tag = match.group(1) # <h1 class="...">
        tag_name = match.group(2) # h1
        text = match.group(3)     # Текст заголовка до блока
        
        # Убираем все пробельные символы (включая переносы) перед принудительным закрытием
        return f"{full_tag}{text.rstrip()}</{tag_name}>"

    content = re.sub(
        fr'(<(h[1-6])(?:\s+[^>]*)?>)((?:(?!</\2>).)*?){block_start_pattern}',
        close_h_callback,
        content,
        flags=re.IGNORECASE | re.DOTALL
    )

    # 2. ORPHAN REMOVAL LOGIC (Глобальная зачистка стеком)
    # Проходим по всем H-тегам. Если встречаем закрывающий тег без открытого парного в стеке — удаляем.
    # Это удаляет как "хвосты", оставшиеся после шага 1, так и любые другие сиротские теги в документе.
    
    h_stack = []
    
    def stack_callback(match):
        raw_tag = match.group(0)
        tag_name = match.group(1).lower()
        # Проверяем, является ли тег закрывающим (с учетом возможных пробелов </ h1>)
        is_closing = raw_tag.lstrip().startswith('</')
        
        if not is_closing:
            # Открытие: кладем в стек
            h_stack.append(tag_name)
            return raw_tag
        else:
            # Закрытие: проверяем вершину стека
            if h_stack and h_stack[-1] == tag_name:
                h_stack.pop()
                return raw_tag
            else:
                # Стек пуст или тег не совпадает с открытым -> это сирота, удаляем
                return ""

    content = re.sub(
        r'</?(h[1-6])(?:\s+[^>]*)?>', 
        stack_callback, 
        content, 
        flags=re.IGNORECASE
    )
    
    return content


# Порог zlib-энтропии, за которым ответ считается вырожденным (зациклившимся).
DEGENERATE_COMPRESSION_THRESHOLD = 0.90
_DEGENERACY_MIN_BYTES = 500
# Хвост режем, только если повтор действительно массовый: иначе легко
# покалечить нормальный текст с парой одинаковых реплик подряд.
_TAIL_MIN_REPEATS = 4
_TAIL_MAX_PERIOD = 2000
_TAIL_MIN_REMOVED_CHARS = 200
# Ниже этого объёма «частичный перевод» не стоит промпта до-генерации.
_MIN_USEFUL_PARTIAL_CHARS = 200


def degenerate_repetition_ratio(text):
    """
    Доля, на которую zlib сжимает текст. Возвращает None, если текст слишком
    короткий, чтобы судить о вырожденности.
    """
    if not text:
        return None
    data = text.encode('utf-8')
    if len(data) <= _DEGENERACY_MIN_BYTES:
        return None
    compressed_size = len(zlib.compress(data))
    return (len(data) - compressed_size) / len(data)


def is_degenerate_repetition(text, threshold=DEGENERATE_COMPRESSION_THRESHOLD):
    """Ответ состоит в основном из повторов одного и того же блока."""
    ratio = degenerate_repetition_ratio(text)
    return ratio is not None and ratio > threshold


def strip_degenerate_repeated_tail(text):
    """
    Отрезает хвост из многократно повторённого блока, оставляя один экземпляр.
    Возвращает (текст, сколько символов удалено).
    """
    if not text:
        return text, 0

    length = len(text)
    max_period = min(_TAIL_MAX_PERIOD, length // _TAIL_MIN_REPEATS)
    for period in range(1, max_period + 1):
        unit = text[length - period:]
        if not unit.strip():
            continue

        repeats = 1
        while (
            length - (repeats + 1) * period >= 0
            and text[length - (repeats + 1) * period:length - repeats * period] == unit
        ):
            repeats += 1

        if repeats < _TAIL_MIN_REPEATS:
            continue

        removed = (repeats - 1) * period
        if removed < _TAIL_MIN_REMOVED_CHARS:
            continue
        return text[:length - removed], removed

    return text, 0


def sanitize_partial_translation(partial_text):
    """
    Готовит «частичный перевод» к повторной отправке в промпт до-генерации.

    Вырожденный хвост отравляет все следующие попытки: модель видит в контексте
    тысячи копий одного абзаца и продолжает ту же петлю, а результат каждый раз
    режется валидатором. Поэтому повтор отрезаем, а если после обрезки текст всё
    ещё вырожденный — выбрасываем хвост целиком и переводим чанк заново.
    """
    if not partial_text or not partial_text.strip():
        return ""

    cleaned, removed = strip_degenerate_repeated_tail(partial_text)
    if is_degenerate_repetition(cleaned):
        return ""
    # После обрезки не осталось ничего осмысленного — значит, зациклился весь
    # ответ, а не только хвост. Продолжать нечего, нужен чистый перевод заново.
    if removed and len(cleaned.strip()) < _MIN_USEFUL_PARTIAL_CHARS:
        return ""
    return cleaned


_PARTIAL_MERGE_MIN_OVERLAP = 24
_PARTIAL_MERGE_MAX_OVERLAP_SCAN = 4000
_PARTIAL_RESTART_PROBE_CHARS = 120


def _partial_head_probe(text, limit=_PARTIAL_RESTART_PROBE_CHARS):
    """Начало текста без разнобоя в пробелах — для сравнения «то же самое начало?»."""
    return re.sub(r'\s+', ' ', (text or "")[:limit * 6]).strip()[:limit]


def merge_partial_with_overlap_guard(previous_text, new_text):
    """
    Приклеивает свежий кусок ответа к уже накопленному частичному переводу.

    Возвращает (склеенный_текст, длина_срезанного_перекрытия).

    Три случая:
    * модель продолжила с места обрыва — просто дописываем;
    * модель повторила хвост накопленного — срезаем перекрытие;
    * модель начала перевод заново — берём более полный из двух вариантов,
      иначе начало главы задвоится и валидатор завалит результат.
    """
    if not previous_text:
        return new_text or "", 0
    if not new_text:
        return previous_text, 0

    previous_probe = _partial_head_probe(previous_text)
    if previous_probe and len(previous_probe) >= _PARTIAL_RESTART_PROBE_CHARS:
        if _partial_head_probe(new_text).startswith(previous_probe):
            return (new_text if len(new_text) >= len(previous_text) else previous_text), 0

    max_overlap = min(len(previous_text), len(new_text), _PARTIAL_MERGE_MAX_OVERLAP_SCAN)
    if max_overlap < _PARTIAL_MERGE_MIN_OVERLAP:
        return previous_text + new_text, 0

    for candidate_text in (new_text, new_text.lstrip()):
        candidate_max_overlap = min(len(previous_text), len(candidate_text), max_overlap)
        for overlap_len in range(candidate_max_overlap, _PARTIAL_MERGE_MIN_OVERLAP - 1, -1):
            if previous_text[-overlap_len:] == candidate_text[:overlap_len]:
                return previous_text + candidate_text[overlap_len:], overlap_len

    return previous_text + new_text, 0


def validate_html_structure(original_html, translated_html):
    """
    Умная валидация ответа с глубокой проверкой структуры.
    Версия 5.0: AUTO-REPAIR. Пытается починить баланс <p>, если он нарушен.
    Возвращает кортеж: (is_valid, reason, final_html_content).
    """
    if not translated_html or not translated_html.strip():
        return False, "API вернуло пустой ответ.", translated_html

    # Один парс на уникальную строку в рамках этого вызова (soup'ы read-only).
    soup_cache = {}
    original_html = _normalize_epub_heading_for_validation(original_html, soup_cache)
    final_translated_html = _normalize_epub_heading_for_validation(translated_html, soup_cache)
    normalized_orig = prettify_html_for_ai(original_html)
    orig_lower = normalized_orig.lower().strip()
    final_translated_html = normalize_translated_body_wrapper(original_html, final_translated_html)
    final_translated_html = normalize_xhtml_tag_case(final_translated_html)
    final_translated_html = _coerce_first_heading_level(original_html, final_translated_html, soup_cache)
    final_translated_html = normalize_dialogue_dashes(final_translated_html)
    final_translated_html = normalize_chapter_heading_format(final_translated_html)
    trans_lower = final_translated_html.lower().strip()

    # --- ПРОВЕРКА 1: Целостность <body> (Regex) ---
    orig_has_body_start = bool(re.search(r'<body\b', orig_lower))
    orig_has_body_end = bool(re.search(r'</body>', orig_lower))

    if orig_has_body_start and orig_has_body_end:
        trans_has_body_start = bool(re.search(r'<body\b', trans_lower))
        trans_has_body_end = bool(re.search(r'</body>', trans_lower))
        
        if not (trans_has_body_start and trans_has_body_end):
            return False, "API не вернуло ожидаемую обертку <body>…</body>.", final_translated_html

    
    # --- НАЧАЛО БЛОКА: Глубокая структурная валидация ---
    try:
        soup_orig = _parse_html_for_validation(normalized_orig, soup_cache)
        soup_orig_raw = _parse_html_for_validation(original_html, soup_cache)
        # Пока парсим оригинал перевода
        soup_trans = _parse_html_for_validation(final_translated_html, soup_cache)

        # ПРОВЕРКА 2.1: Фундаментальные теги. Body-only output is valid here:
        # the pipeline intentionally coerces translated XHTML to a <body> block.
        tags_to_check = {'<html>': '<html', '</html>': '</html>'}
        for display_name, search_string in tags_to_check.items():
            trans_has_body_wrapper = bool(re.search(r'<body\b', trans_lower)) and bool(re.search(r'</body>', trans_lower))
            if (search_string in orig_lower) and not (search_string in trans_lower) and not trans_has_body_wrapper:
                return False, f"Потерян фундаментальный тег {display_name.replace('<', '&lt;')}. Ответ API поврежден.", final_translated_html

        # ПРОВЕРКА 2.2: Умный баланс тегов <p> через Regex
        p_open_pat = r'<p(?:\s|>)'
        p_close_pat = r'</p>'

        orig_p_open = len(re.findall(p_open_pat, orig_lower))
        orig_p_close = len(re.findall(p_close_pat, orig_lower))
        p_balance_orig = orig_p_open - orig_p_close

        # Считаем баланс перевода
        trans_p_open = len(re.findall(p_open_pat, trans_lower))
        trans_p_close = len(re.findall(p_close_pat, trans_lower))
        p_balance_trans = trans_p_open - trans_p_close

        # === AUTO-REPAIR LOGIC ===
        # Лечим только при соблюдении трех условий:
        # 1. В оригинале баланс идеален (0).
        # 2. В переводе баланс нарушен (не 0).
        # 3. В оригинале вообще есть теги <p> (чтобы не лечить голый текст или div-верстку).
        if p_balance_orig == 0 and p_balance_trans != 0 and orig_p_open > 0:
            # Пытаемся лечить
            repaired_html = repair_unbalanced_paragraphs(final_translated_html)
            
            # Проверяем баланс на вылеченной версии
            repaired_lower = repaired_html.lower()
            rep_p_open = len(re.findall(p_open_pat, repaired_lower))
            rep_p_close = len(re.findall(p_close_pat, repaired_lower))
            p_balance_repaired = rep_p_open - rep_p_close
            
            # Если лечение помогло (баланс стал идеальным, как в оригинале)
            if p_balance_repaired == 0:
                final_translated_html = repaired_html
                # Обновляем soup для последующих структурных проверок
                soup_trans = _parse_html_for_validation(final_translated_html, soup_cache)
                # И обновляем lower версию для проверок тегов
                trans_lower = repaired_lower
                # Обновляем текущий баланс, чтобы пройти финальную проверку ниже
                p_balance_trans = 0

        paragraph_repaired_html = repair_missing_paragraph_tags(original_html, final_translated_html, soup_cache)
        if paragraph_repaired_html != final_translated_html:
            final_translated_html = paragraph_repaired_html
            soup_trans = _parse_html_for_validation(final_translated_html, soup_cache)
            trans_lower = final_translated_html.lower().strip()
            trans_p_open = len(re.findall(p_open_pat, trans_lower))
            trans_p_close = len(re.findall(p_close_pat, trans_lower))
            p_balance_trans = trans_p_open - trans_p_close

        # Финальная проверка баланса (после попытки лечения или если лечение не применялось)
        if p_balance_orig != p_balance_trans:
            return False, f"Нарушен баланс тегов <p> (в оригинале {p_balance_orig}, в переводе {p_balance_trans}). Возможно, потерян закрывающий тег.", final_translated_html

        # ПРОВЕРКА 2.3: Сравнение "отпечатков" структуры
        orig_leading_text = _find_leading_visible_text_before_expected_block(soup_orig_raw)
        trans_leading_text = _find_leading_visible_text_before_expected_block(soup_trans)
        if not orig_leading_text and trans_leading_text:
            repaired_html, repaired = _repair_leading_visible_text_before_expected_block(
                soup_orig_raw,
                final_translated_html,
            )
            if repaired:
                final_translated_html = repaired_html
                soup_trans = _parse_html_for_validation(final_translated_html, soup_cache)
                trans_lower = final_translated_html.lower().strip()
                trans_leading_text = _find_leading_visible_text_before_expected_block(soup_trans)
            if trans_leading_text:
                return False, (
                    "Leading stray text appeared inside <body> before the first expected block "
                    "(<h1>-<h6>/<p>/<div>): "
                    f"{trans_leading_text!r}"
                ), final_translated_html

        orig_fp = _create_structural_fingerprint(soup_orig)
        trans_fp = _create_structural_fingerprint(soup_trans)
        
        for h in set(orig_fp['headings'].keys()) | set(trans_fp['headings'].keys()):
            orig_h_count = orig_fp['headings'].get(h, 0)
            trans_h_count = trans_fp['headings'].get(h, 0)
            if orig_h_count != trans_h_count:
                if not (orig_h_count == 0 and trans_h_count == 1):
                    return False, f"Несоответствие тегов <{h}>: {orig_h_count} в оригинале vs {trans_h_count} в переводе.", final_translated_html
        
        for tag_key, tag_name in [('links', '<a>'), ('lists', '<ol/ul>')]:
            if orig_fp[tag_key] != trans_fp[tag_key]:
                return False, f"Несоответствие тегов {tag_name}: {orig_fp[tag_key]} в оригинале vs {trans_fp[tag_key]} в переводе.", final_translated_html

    except Exception as e:
        return False, f"Ошибка при анализе HTML-структуры: {e}", final_translated_html

    # --- ПРОВЕРКА 3: Эвристика ---
    text_orig = soup_orig.get_text() if 'soup_orig' in locals() else normalized_orig
    text_trans = soup_trans.get_text() if 'soup_trans' in locals() else final_translated_html

    if len(text_orig) > 3000:
        text_ratio = len(text_trans) / max(len(text_orig), 1)
        min_ratio = _large_translation_min_ratio(text_orig)
        if text_ratio < min_ratio:
            return False, (
                "Translation is too short for a large source "
                f"(ratio {text_ratio:.2f} < {min_ratio:.2f})."
            ), final_translated_html

        orig_p_count = _paragraph_tag_count(original_html)
        trans_p_count = _paragraph_tag_count(final_translated_html)
        min_p_count = max(3, int(orig_p_count * 0.30))
        if orig_p_count >= 12 and trans_p_count < min_p_count:
            return False, (
                "Too many source paragraphs collapsed or disappeared "
                f"({trans_p_count}/{orig_p_count} < {min_p_count})."
            ), final_translated_html

    try:
        compression_ratio = degenerate_repetition_ratio(final_translated_html)
        if compression_ratio is not None and compression_ratio > DEGENERATE_COMPRESSION_THRESHOLD:
            return False, f"Аномально высокая степень сжатия ({compression_ratio:.1%}).", final_translated_html
    except Exception as e:
        print(f"[WARN] Ошибка при проверке сжатия: {e}")
    
    if len(text_orig) > 500 and len(text_trans) < len(text_orig) / 8:
        return False, f"Ответ слишком короткий.", final_translated_html
        
    if len(text_trans) > 100 and not re.search(r'[а-яА-ЯёЁ]', text_trans):
        return False, "Ответ не содержит кириллицы.", final_translated_html
        
    return True, "OK", final_translated_html
    
def clean_html_content(html_content, is_html=False):
    """
    Очищает ответ от API, извлекая контент между первой и последней ``` оберткой.
    Версия 12.0 ("Экстрактор v2"): Надежный алгоритм на основе find/rfind.
    """
    if not html_content or not isinstance(html_content, str):
        return ""

    text = html_content.strip()
    
    # 1. Находим позиции первого и последнего маркера
    first_marker_pos = text.find('```')
    last_marker_pos = text.rfind('```')

    extracted_content = ""

    # 2. Применяем простую и надежную логику
    if first_marker_pos == -1:
        # Случай 1: Маркеров нет вообще. Весь текст - полезная нагрузка.
        extracted_content = text
    elif first_marker_pos == last_marker_pos:
        # --- НАЧАЛО ИЗМЕНЕНИЯ: УМНАЯ ОБРАБОТКА ЕДИНСТВЕННОГО МАРКЕРА ---
        # Если маркер в первой половине строки - считаем его открывающим.
        if first_marker_pos < len(text) / 2:
            open_match = re.search(r'```(?:json|html|xml|xhtml)?\s*\n?', text)
            start_pos = open_match.end() if open_match else first_marker_pos + 3
            extracted_content = text[start_pos:]
        # Если маркер во второй половине строки - считаем его закрывающим.
        else:
            extracted_content = text[:first_marker_pos]
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    else:
        # Случай 3: Найдено два или больше маркеров. Берем все между первым и последним.
        open_match = re.search(r'```(?:json|html|xml|xhtml)?\s*\n?', text)
        start_pos = open_match.end() if open_match else first_marker_pos + 3
        end_pos = last_marker_pos
        extracted_content = text[start_pos:end_pos]
        
    # 3. Финальная очистка от любых остаточных маркеров внутри блока
    cleaned = re.sub(r'```(?:json|html|xml|xhtml)?\s*|\s*```', '', extracted_content).strip()

    # 4. Если это полноценный HTML, извлекаем из него <body>
    if is_html:
        return process_body_tag(cleaned, return_parts=False, body_content_only=False)

    # 5. Для JSON или пакетной обработки возвращаем просто очищенный блок
    return cleaned

def is_content_effectively_empty(html_content):
    """
    Проверяет, содержит ли HTML хоть какой-то значимый текст для перевода.
    Игнорирует теги, комментарии (включая плейсхолдеры медиа) и пробелы.
    """
    if not html_content or not html_content.strip():
        return True
        
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # get_text() игнорирует комментарии (<!-- MEDIA_0 -->) и теги.
        text = soup.get_text(strip=True)
        return not bool(text)
    except Exception:
        # Если парсинг не удался, на всякий случай считаем, что контент есть,
        # чтобы не потерять данные.
        return False
