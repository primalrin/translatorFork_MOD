# build_master.py (v15.0 - "The Universal Collector")
# Улучшение: ADDITIONAL_DATA теперь универсальна.
# Можно добавлять и папки ('config'), и отдельные файлы ('README.md', 'data/dict.txt').
# Скрипт сам определит тип и сгенерирует корректные команды для PyInstaller и xcopy/copy.

import os
import sys
import ast
import importlib.util
from pathlib import Path
import re

# --- ОБЩАЯ КОНФИГУРАЦИЯ ---
PROJECT_ROOT = Path(__file__).parent.resolve()
MAIN_PY_FILE = "main.py"
APP_ICON_FILE = "gemini_translator\\GT.ico"
OUTPUT_BAT_FILE = "build.bat"
OUTPUT_REQUIREMENTS_FILE = "requirements.txt"

# <-- НОВАЯ УНИВЕРСАЛЬНАЯ ПЕРЕМЕННАЯ
# Указывайте здесь папки ИЛИ отдельные файлы, которые должны
# попасть в итоговую сборку с сохранением путей.
ADDITIONAL_DATA = [
    ('config', 'config'),
    ('README.md', '.'),
    ('ffmpeg.exe', '.'),
    ('ffprobe.exe', '.'),
    ('gemini_translator\\scripts\\chatgpt_workascii_bridge.cjs', 'gemini_translator\\scripts'),
    ('gemini_translator\\scripts\\chatgpt_profile_launcher.cjs', 'gemini_translator\\scripts'),
]

EXCLUDE_DIRS = {'venv', '.venv', 'env', '.git', '__pycache__', 'dist', 'build'}
PROJECT_MODULES = {
    'gemini_translator',
    'gemini_reader_v3',
    'main',
    'init',
    'os_patch',
    'api_upload',
    'constants',
    'dependencies',
    'dialogs',
    'main_window',
    'models',
    'parsers',
    'window_branding',
    'utils',
    'workers',
}
DEV_MODULES = {'pyinstaller', 'pyinstaller-hooks-contrib'}
DATA_FILE_EXTENSIONS = {'.txt', '.json', '.ico', '.css', '.html', '.js'}
# RanobeLib загружается из bundled source-файлов, поэтому PyInstaller
# не видит его import playwright.sync_api во время анализа main.py.
HIDDEN_IMPORTS_BLOCK = ['PyQt6.sip', 'docx', 'playwright.sync_api', 'google.genai', 'google.genai.types']
MANUAL_COLLECT_DATA_MODULES = {'certifi', 'docx'}
COLLECT_DATA_EXCLUDE_MODULES = {'setuptools'}
MANUALLY_PACKAGED_PACKAGES = {'playwright'}
# --- КОНФИГУРАЦИЯ ЗАВИСИМОСТЕЙ ---
IMPORT_TO_PACKAGE_MAP = {
    'socks': 'PySocks',
    'opencc': 'opencc-python-reimplemented',
    'Levenshtein': 'python-Levenshtein',
    'jwt': 'pyjwt',
    'bs4': 'beautifulsoup4',
    'docx': 'python-docx',
    'ebooklib': 'EbookLib',
    'edge_tts': 'edge-tts',
    'google': 'google-genai',
    'pyaudio': 'PyAudio',
    'pymorphy2': 'pymorphy3',
    'recognizers_text': 'recognizers-text',
    'recognizers_number': 'recognizers-text-number',
}

ESSENTIAL_PACKAGES = {
    'playwright',
    'python-docx',
    'EbookLib',
    'nltk',
    'PyAudio',
    'pydub',
    'edge-tts',
    'google-genai',
    'loguru',
    'websockets',
}
FORCED_VERSIONS = {
    'pydantic': '>=2.0.0',
    'setuptools': '<81',
}
CONFLICTING_PACKAGES_TO_REMOVE = {"os_patch", "pyinstaller_hooks_contrib"}


def normalize_data_entry(entry):
    if isinstance(entry, (str, Path)):
        source = Path(entry)
        if source.is_dir():
            destination = Path(source.name)
        else:
            destination = source.parent if str(source.parent) != '.' else Path('.')
        return source, Path(destination)

    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        source, destination = entry
        return Path(source), Path(destination)

    raise ValueError(f"Неподдерживаемый элемент ADDITIONAL_DATA: {entry!r}")


def discover_playwright_runtime_data():
    discovered = []
    project_runtime_dir = PROJECT_ROOT / "playwright_runtime"
    project_node_path = project_runtime_dir / "node.exe"
    project_package_dir = project_runtime_dir / "package"
    project_browser_cache = project_runtime_dir / "ms-playwright"

    if project_node_path.exists():
        discovered.append((project_node_path, Path("playwright_runtime")))
    if project_package_dir.exists():
        discovered.append((project_package_dir, Path("playwright_runtime") / "package"))
    if project_browser_cache.exists():
        discovered.append((project_browser_cache, Path("playwright_runtime") / "ms-playwright"))

    try:
        spec = importlib.util.find_spec("playwright")
    except Exception:
        spec = None

    if spec and spec.origin:
        driver_dir = Path(spec.origin).parent / "driver"
        node_path = driver_dir / "node.exe"
        package_dir = driver_dir / "package"

        if project_node_path.exists():
            pass
        elif node_path.exists():
            discovered.append((node_path, Path("playwright_runtime")))
        else:
            print("     [WARN] Playwright driver node.exe не найден.")

        if project_package_dir.exists():
            pass
        elif package_dir.exists():
            discovered.append((package_dir, Path("playwright_runtime") / "package"))
        else:
            print("     [WARN] Playwright driver package не найден.")
    else:
        print("     [WARN] Python-пакет 'playwright' не найден, bundled runtime не будет добавлен.")

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        browser_cache = Path(localappdata) / "ms-playwright"
        if project_browser_cache.exists():
            pass
        elif browser_cache.exists():
            discovered.append((browser_cache, Path("playwright_runtime") / "ms-playwright"))
        else:
            print("     [WARN] Локальный cache ms-playwright не найден, bundled browser cache будет пропущен.")
    else:
        print("     [WARN] Переменная LOCALAPPDATA не задана, bundled browser cache будет пропущен.")

    return discovered


def discover_ranobelib_source_data():
    candidate_dirs = [PROJECT_ROOT / "ranobelib"]

    for base_dir in candidate_dirs:
        if not (base_dir / "main_window.py").exists():
            continue

        discovered = []
        for file_path in sorted(base_dir.iterdir()):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".py", ".mjs"}:
                continue
            if ".bak" in file_path.name:
                continue
            discovered.append((file_path, Path("ranobelib")))

        if discovered:
            return discovered

    print("     [WARN] Исходники RanobeLib не найдены, в сборку они не попадут.")
    return []


def get_additional_data_entries():
    entries = list(ADDITIONAL_DATA)
    entries.extend(discover_ranobelib_source_data())
    entries.extend(discover_playwright_runtime_data())
    return entries


def find_project_imports():
    print("--- Этап 1: Сканирование файлов проекта для поиска импортов ---")
    all_imports = set()
    for file_path in PROJECT_ROOT.rglob("*.py"):
        if any(part in file_path.parts for part in EXCLUDE_DIRS): continue
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as f: content = f.read()
            tree = ast.parse(content, filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names: all_imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0: continue
                    if node.module: all_imports.add(node.module.split('.')[0])
        except Exception as e:
            print(f"  [Предупреждение] Не удалось проанализировать {file_path}: {e}")
    print(f"[OK] Найдено {len(all_imports)} уникальных модулей.")
    return all_imports

def filter_third_party_imports(imports):
    print("\n--- Этап 2: Фильтрация модулей (улучшенная логика) ---")
    third_party_imports = set()
    
    # 1. Получаем список стандартных библиотек. Это наш "черный список".
    try:
        # Для Python 3.10+
        standard_libs = set(sys.stdlib_module_names)
        print(f"  -> Используется полный список стандартных библиотек Python {sys.version.split()[0]}.")
    except AttributeError:
        # Для более старых версий Python (fallback)
        standard_libs = set(sys.builtin_module_names)
        print(f"  -> [WARN] Используется базовый список встроенных модулей. Точность может быть ниже.")

    # 2. Итерируем по всем найденным импортам
    for module_name in sorted(list(imports)):
        # 3. Применяем простое правило исключения
        if module_name in PROJECT_MODULES or module_name in standard_libs:
            # Если модуль - часть нашего проекта или стандартный, пропускаем его.
            continue
        
        # 4. ВСЁ ОСТАЛЬНОЕ - считаем сторонней зависимостью!
        third_party_imports.add(module_name)

    print("[OK] Идентифицированы сторонние зависимости по принципу исключения.")
    return third_party_imports

def apply_package_mapping(dependencies):
    print("\n--- Этап 3: Применение карты 'импорт -> пакет' ---")
    remapped_deps = set()
    for dep in dependencies:
        if dep in IMPORT_TO_PACKAGE_MAP:
            package_name = IMPORT_TO_PACKAGE_MAP[dep]
            remapped_deps.add(package_name)
            print(f"  -> Переназначен импорт '{dep}' на пакет '{package_name}'.")
        else:
            remapped_deps.add(dep)
    print("[OK] Переназначение завершено.")
    return remapped_deps

def update_requirements_file(dependencies):
    print(f"\n--- Этап 4: Обновление '{OUTPUT_REQUIREMENTS_FILE}' ---")
    filtered_deps = dependencies - DEV_MODULES - CONFLICTING_PACKAGES_TO_REMOVE
    final_dependencies = set()
    for dep in filtered_deps:
        dep_lower = dep.lower()
        if dep_lower in FORCED_VERSIONS:
            final_dependencies.add(f"{dep}{FORCED_VERSIONS[dep_lower]}")
        else:
            final_dependencies.add(dep)
    sorted_deps = sorted(list(final_dependencies), key=str.lower)
    try:
        with open(OUTPUT_REQUIREMENTS_FILE, 'w', encoding='utf-8') as f:
            f.write("# Сгенерировано автоматически скриптом build_master.py\n")
            f.write("\n".join(sorted_deps) + "\n")
        print(f"[OK] Файл '{OUTPUT_REQUIREMENTS_FILE}' успешно обновлен.")
        return [re.split(r'[>=<]', dep)[0] for dep in sorted_deps]
    except Exception as e:
        print(f"[ОШИБКА] Не удалось записать в '{OUTPUT_REQUIREMENTS_FILE}': {e}")
        return []

def analyze_dependencies_for_pyinstaller_flags(dependencies):
    print(f"\n--- Этап 5: Анализ пакетов для PyInstaller ---")
    collect_data_flags = set()
    for package_name in dependencies:
        if package_name in MANUALLY_PACKAGED_PACKAGES or package_name in COLLECT_DATA_EXCLUDE_MODULES:
            continue
        try:
            spec = importlib.util.find_spec(package_name)
            if not spec or not spec.origin: continue
            package_dir = Path(spec.origin).parent
            has_data_files = any(
                fp.is_file() and fp.suffix.lower() in DATA_FILE_EXTENSIONS
                for fp in package_dir.rglob('*')
                if '.dist-info' not in fp.parts and '.egg-info' not in fp.parts
            )
            if has_data_files:
                collect_data_flags.add(package_name)
        except Exception: pass
    if collect_data_flags:
        print(f"  -> Обнаружены и добавлены флаги сбора для: {', '.join(collect_data_flags)}")
    return collect_data_flags

def generate_pure_bat_script(dependencies, collect_data_flags):
    print(f"\n--- Этап 6: Генерация универсального лаунчера '{OUTPUT_BAT_FILE}' ---")
    
    hooks_block = []
    try:
        import pyinstaller_hooks_contrib
        hooks_path = pyinstaller_hooks_contrib.get_hook_dirs()[0]
        hooks_block.append(f'--additional-hooks-dir="{hooks_path}"')
        print("[OK] Найдены хуки сообщества (pyinstaller-hooks-contrib).")
    except (ImportError, IndexError):
        print("[ПРЕДУПРЕЖДЕНИЕ] pyinstaller-hooks-contrib не найден.")
    
    collect_data_modules = set(collect_data_flags)
    collect_data_modules.update(MANUAL_COLLECT_DATA_MODULES)
    data_block = [f'--collect-data="{data}"' for data in sorted(list(collect_data_modules))]
    
    base_pyinstaller_args = [f'"%PYTHON_CMD%" -m PyInstaller {MAIN_PY_FILE}', "--windowed",
        '--name="%AppName%"', "--clean", f'--icon="{APP_ICON_FILE}"', "--noconfirm"]
    base_pyinstaller_args.extend(hooks_block)
    base_pyinstaller_args.extend(data_block)
    
    hidden_imports_args = [f'--hidden-import="{imp}"' for imp in HIDDEN_IMPORTS_BLOCK]
    base_pyinstaller_args.extend(hidden_imports_args)

    # --- НОВАЯ ЛОГИКА ДЛЯ DATA ---
    # Эти аргументы теперь будут ОБЩИМИ для ВСЕХ режимов сборки.
    # Все файлы из ADDITIONAL_DATA всегда упаковываются внутрь.
    add_data_args = []
    copy_commands_hybrid = []
    copy_commands_advanced = []
    
    print("  -> Анализ ADDITIONAL_DATA для включения в сборку:")
    for item in get_additional_data_entries():
        path, destination = normalize_data_entry(item)
        if not path.exists():
            print(f"     [WARN] Элемент не найден и будет пропущен: {path}")
            continue

        destination_str = str(destination) if str(destination) else '.'
        display_source = str(path)
        if path.is_dir():
            print(f"     - Папка: {display_source} -> {destination_str}")
        else:
            print(f"     - Файл:  {display_source} -> {destination_str}")

        add_data_args.append(f'--add-data "{path};{destination_str}"')

        src_win = str(path).replace('/', '\\')
        dest_win = destination_str.replace('/', '\\')

        if path.is_dir():
            copy_commands_hybrid.append(f'    xcopy "{src_win}" "dist\\{dest_win}\\" /E /I /Y /Q > nul')
            copy_commands_advanced.append(f'    xcopy "{src_win}" "dist\\%AppName%\\{dest_win}\\" /E /I /Y /Q > nul')
        else:
            if dest_win not in ("", "."):
                copy_commands_hybrid.append(f'    if not exist "dist\\{dest_win}" mkdir "dist\\{dest_win}"')
                copy_commands_hybrid.append(f'    copy /Y "{src_win}" "dist\\{dest_win}\\{path.name}" > nul')
                copy_commands_advanced.append(f'    if not exist "dist\\%AppName%\\{dest_win}" mkdir "dist\\%AppName%\\{dest_win}"')
                copy_commands_advanced.append(f'    copy /Y "{src_win}" "dist\\%AppName%\\{dest_win}\\{path.name}" > nul')
            else:
                copy_commands_hybrid.append(f'    copy /Y "{src_win}" "dist\\{path.name}" > nul')
                copy_commands_advanced.append(f'    copy /Y "{src_win}" "dist\\%AppName%\\{path.name}" > nul')

    # Команда для ПОЛНОСТЬЮ ПОРТАТИВНОЙ сборки (включает --add-data)
    full_portable_args = list(base_pyinstaller_args) + ["--onefile"] + add_data_args
    pyinstaller_command_full_portable = " ^\n".join(full_portable_args)

    # Команда для ГИБРИДНОЙ сборки (теперь ТОЖЕ включает --add-data)
    hybrid_args = list(base_pyinstaller_args) + ["--onefile"]
    pyinstaller_command_hybrid = " ^\n".join(hybrid_args)
    
    # Команда для ПРОДВИНУТОЙ сборки (теперь ТОЖЕ включает --add-data)
    advanced_args = list(base_pyinstaller_args)
    pyinstaller_command_advanced = " ^\n".join(advanced_args)
    
    hybrid_copy_block = "\n".join(copy_commands_hybrid)
    advanced_copy_block = "\n".join(copy_commands_advanced)
    clean_bat_content = f"""@echo off
chcp 65001 >nul
setlocal
cls
goto setup_env

:: ============================================================================
:: Универсальный лаунчер GeminiTranslator
:: Сгенерировано: build_master.py (v15.0 - "The Universal Collector")
:: ============================================================================

:: --- Этап 1: Проверка и запрос прав администратора (если нужно) ---
>nul 2>&1 net session
if '%errorlevel%' NEQ '0' (
    echo.
    echo [+] Запрос прав администратора...
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\\getadmin.vbs"
    cscript "%temp%\\getadmin.vbs" & exit /B
)
if exist "%temp%\\getadmin.vbs" ( del "%temp%\\getadmin.vbs" )

:: --- Этап 2: Настройка рабочего окружения ---
:setup_env
cd /d "%~dp0"
if exist "%cd%\\.venv\\Scripts\\python.exe" (
    set "PYTHON_CMD=%cd%\\.venv\\Scripts\\python.exe"
) else if exist "%cd%\\venv\\Scripts\\python.exe" (
    set "PYTHON_CMD=%cd%\\venv\\Scripts\\python.exe"
) else (
    set "PYTHON_CMD=python"
)

if /I "%PYTHON_CMD%"=="python" (
    where python >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [!!!] Python не найден. Установите Python или создайте локальный .venv рядом с проектом.
        pause
        goto :eof
    )
) else if not exist "%PYTHON_CMD%" (
    echo [!!!] Не найден интерпретатор Python: %PYTHON_CMD%
    pause
    goto :eof
)

echo [+] Используется Python: %PYTHON_CMD%
echo [+] Рабочая директория: %cd%

for %%I in ("%cd%") do set "AppName=%%~nxI"
echo [+] Имя приложения будет: %AppName%
echo.

:: --- Главное меню ---
:menu
cls
echo ======================================================
echo   Универсальный лаунчер для: %AppName%
echo ======================================================
echo.
echo   1. Установить / Обновить зависимости программы
echo.
echo   2. Собрать приложение
echo.
echo   3. Выход
echo.
echo ======================================================
set /p choice="Выберите действие (1, 2 или 3): "

if not defined choice ( goto menu )
if "%choice%"=="1" ( goto install_deps )
if "%choice%"=="2" ( goto build_menu )
if "%choice%"=="3" ( goto :eof )

echo Неверный выбор. Пожалуйста, введите 1, 2 или 3.
pause
goto menu

:: --- Меню сборки ---
:build_menu
cls
echo ======================================================
echo   Выберите тип сборки
echo ======================================================
echo.
echo   1. ПОЛНОСТЬЮ ПОРТАТИВНАЯ (один .exe файл)
echo      - Создает один .exe файл. Все встроено внутрь.
echo      - Легко распространять, но настройки менять нельзя.
echo      - Рекомендуется для большинства пользователей.
echo.
echo   2. ГИБРИДНАЯ (один .exe + папки с данными)
echo      - Создает один .exe и рядом с ним папки с данными.
echo      - Сочетает портативность и возможность менять конфиги.
echo      - Рекомендуется для опытных пользователей.
echo.
echo   3. ПРОДВИНУТАЯ (папка с файлами)
echo      - Создает папку с .exe и всеми зависимостями.
echo      - Позволяет вручную редактировать конфиги и данные.
echo      - Для разработчиков и отладки.
echo.
echo   4. Назад в главное меню
echo.
echo ======================================================
set /p build_choice="Выберите действие (1, 2, 3 или 4): "

if not defined build_choice ( goto build_menu )
if "%build_choice%"=="1" ( goto build_full_portable )
if "%build_choice%"=="2" ( goto build_hybrid )
if "%build_choice%"=="3" ( goto build_advanced )
if "%build_choice%"=="4" ( goto menu )

echo Неверный выбор.
pause
goto build_menu


:: --- Блок установки зависимостей ---
:install_deps
cls
echo --- Установка / обновление зависимостей программы ---
echo.
echo [+] Запуск установки из файла '{OUTPUT_REQUIREMENTS_FILE}'...
"%PYTHON_CMD%" -m pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}"
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке. Проверьте подключение к интернету.
) else (
    echo [OK] Все зависимости успешно установлены/обновлены.
)
echo.
pause
goto :eof


:: --- Блок сборки: ПОЛНОСТЬЮ ПОРТАТИВНАЯ ---
:build_full_portable
call :build_app_base "ПОЛНОСТЬЮ ПОРТАТИВНАЯ"
{pyinstaller_command_full_portable}
call :build_app_end
goto :eof


:: --- Блок сборки: ГИБРИДНАЯ ---
:build_hybrid
call :build_app_base "ГИБРИДНАЯ"
{pyinstaller_command_hybrid}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{hybrid_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
{pyinstaller_command_advanced}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{advanced_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- Общая логика сборки ---
:build_app_base
cls
echo --- Полный цикл сборки (%~1 версия) ---
echo.
echo [+] Этап 1 из 3: Установка/обновление всех зависимостей и инструментов...
"%PYTHON_CMD%" -m pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}" pyinstaller pyinstaller-hooks-contrib
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке зависимостей. Проверьте подключение к интернету.
    pause
    goto menu
)
if exist "dist\\chatgpt-profile-run" rmdir /S /Q "dist\\chatgpt-profile-run"
if exist "dist\\logs" rmdir /S /Q "dist\\logs"
if exist "dist\\%AppName%\\chatgpt-profile-run" rmdir /S /Q "dist\\%AppName%\\chatgpt-profile-run"
if exist "dist\\%AppName%\\logs" rmdir /S /Q "dist\\%AppName%\\logs"
echo [+] Инструменты для сборки готовы.
echo.
echo [+] Этап 2 из 3: Запуск PyInstaller для сборки "%AppName%"...
echo.
goto :eof

:build_app_end
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!!!] СБОРКА ЗАВЕРШИЛАСЬ С ОШИБКОЙ!
    echo     Просмотрите сообщения выше, чтобы найти причину.
) else (
    echo.
    echo [OK] СБОРКА УСПЕШНО ЗАВЕРШЕНА!
    echo     Готовое приложение находится в папке 'dist'.
)
echo.
echo [+] Процесс завершен.
pause
goto :eof

"""

    bat_content = f"""@echo off
setlocal
cls

:: ============================================================================
:: Универсальный лаунчер GeminiTranslator
:: Сгенерировано: build_master.py (v15.0 - "The Universal Collector")
:: ============================================================================

:: --- Этап 1: Проверка и запрос прав администратора (если нужно) ---
>nul 2>&1 net session
if '%errorlevel%' NEQ '0' (
    echo.
    echo [+] Запрос прав администратора...
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\\getadmin.vbs"
    cscript "%temp%\\getadmin.vbs" & exit /B
)
if exist "%temp%\\getadmin.vbs" ( del "%temp%\\getadmin.vbs" )

:: --- Этап 2: Настройка рабочего окружения ---
cd /d "%~dp0"
echo [+] Рабочая директория: %cd%

for %%I in ("%cd%") do set "AppName=%%~nxI"
echo [+] Имя приложения будет: %AppName%
echo.

:: --- Главное меню ---
:menu
cls
echo ======================================================
echo   Универсальный лаунчер для: %AppName%
echo ======================================================
echo.
echo   1. Установить / Обновить зависимости программы
echo.
echo   2. Собрать приложение
echo.
echo   3. Выход
echo.
echo ======================================================
set /p choice="Выберите действие (1, 2 или 3): "

if not defined choice ( goto menu )
if "%choice%"=="1" ( goto install_deps )
if "%choice%"=="2" ( goto build_menu )
if "%choice%"=="3" ( goto :eof )

echo Неверный выбор. Пожалуйста, введите 1, 2 или 3.
pause
goto menu

:: --- Меню сборки ---
:build_menu
cls
echo ======================================================
echo   Выберите тип сборки
echo ======================================================
echo.
echo   1. ПОЛНОСТЬЮ ПОРТАТИВНАЯ (один .exe файл)
echo      - Создает один .exe файл. Все встроено внутрь.
echo      - Легко распространять, но настройки менять нельзя.
echo      - Рекомендуется для большинства пользователей.
echo.
echo   2. ГИБРИДНАЯ (один .exe + папки с данными)
echo      - Создает один .exe и рядом с ним папки с данными.
echo      - Сочетает портативность и возможность менять конфиги.
echo      - Рекомендуется для опытных пользователей.
echo.
echo   3. ПРОДВИНУТАЯ (папка с файлами)
echo      - Создает папку с .exe и всеми зависимостями.
echo      - Позволяет вручную редактировать конфиги и данные.
echo      - Для разработчиков и отладки.
echo.
echo   4. Назад в главное меню
echo.
echo ======================================================
set /p build_choice="Выберите действие (1, 2, 3 или 4): "

if not defined build_choice ( goto build_menu )
if "%build_choice%"=="1" ( goto build_full_portable )
if "%build_choice%"=="2" ( goto build_hybrid )
if "%build_choice%"=="3" ( goto build_advanced )
if "%build_choice%"=="4" ( goto menu )

echo Неверный выбор.
pause
goto build_menu


:: --- Блок установки зависимостей ---
:install_deps
cls
echo --- Установка / обновление зависимостей программы ---
echo.
echo [+] Запуск установки из файла '{OUTPUT_REQUIREMENTS_FILE}'...
pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}"
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке. Проверьте подключение к интернету.
) else (
    echo [OK] Все зависимости успешно установлены/обновлены.
)
echo.
pause
goto :eof


:: --- Блок сборки: ПОЛНОСТЬЮ ПОРТАТИВНАЯ ---
:build_full_portable
call :build_app_base "ПОЛНОСТЬЮ ПОРТАТИВНАЯ"
{pyinstaller_command_full_portable}
call :build_app_end
goto :eof


:: --- Блок сборки: ГИБРИДНАЯ ---
:build_hybrid
call :build_app_base "ГИБРИДНАЯ"
{pyinstaller_command_hybrid}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{hybrid_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
{pyinstaller_command_advanced}
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] Этап 3 из 3: Копирование внешних данных...
{advanced_copy_block}
    echo [OK] Данные скопированы.
)
call :build_app_end
goto :eof


:: --- ОБЩАЯ ЛОГИКА СБОРКИ ---
:build_app_base
cls
echo --- Полный цикл сборки (%~1 версия) ---
echo.
echo [+] Этап 1 из 3: Установка/обновление всех зависимостей и инструментов...
pip install --upgrade -r "{OUTPUT_REQUIREMENTS_FILE}" pyinstaller pyinstaller-hooks-contrib
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке зависимостей. Проверьте подключение к интернету.
    pause
    goto menu
)
echo [+] Инструменты для сборки готовы.
echo.
echo [+] Этап 2 из 3: Запуск PyInstaller для сборки "%AppName%"...
echo.
goto :eof

:build_app_end
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!!!] СБОРКА ЗАВЕРШИЛАСЬ С ОШИБКОЙ!
    echo     Просмотрите сообщения выше, чтобы найти причину.
) else (
    echo.
    echo [OK] СБОРКА УСПЕШНО ЗАВЕРШЕНА!
    echo     Готовое приложение находится в папке 'dist'.
)
echo.
echo [+] Процесс завершен.
pause
goto :eof

"""
    
    try:
        with open(OUTPUT_BAT_FILE, 'w', encoding='utf-8', newline='\r\n') as f: f.write(clean_bat_content)
        print(f"[OK] Универсальный лаунчер '{OUTPUT_BAT_FILE}' успешно сгенерирован.")
    except Exception as e:
        print(f"[ОШИБКА] Не удалось записать файл '{OUTPUT_BAT_FILE}': {e}")

if __name__ == "__main__":
    all_imports = find_project_imports()
    third_party_deps = filter_third_party_imports(all_imports)
    remapped_deps = apply_package_mapping(third_party_deps)
    
    print(f"\n--- Применение правил из конфигурации ---")
    remapped_deps.update(ESSENTIAL_PACKAGES)
    remapped_deps.update(FORCED_VERSIONS.keys())
    print(f"  -> Добавлены обязательные пакеты.")
    
    print(f"\nИтоговый список зависимостей: {', '.join(sorted(list(remapped_deps)))}")
    final_deps_names = update_requirements_file(remapped_deps)
    if final_deps_names:
        data_flags = analyze_dependencies_for_pyinstaller_flags(final_deps_names)
        generate_pure_bat_script(final_deps_names, data_flags)
        print("\n" + "="*60 + "\n[ГОТОВО] УНИВЕРСАЛЬНЫЙ ЛАУНЧЕР ГОТОВ!\n" + "="*60)
