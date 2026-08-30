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

from pyinstaller_config import LAZY_HANDLER_HIDDEN_IMPORTS, LAZY_SERVER_HIDDEN_IMPORTS

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
    ('qidian_rulate\\tags.txt', 'qidian_rulate'),
    ('tools\\tomato', 'tools\\tomato'),
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
# qoder_agent_sdk импортируется лениво (при первом обращении к Qoder),
# поэтому PyInstaller не видит его при анализе — нужен явный hidden-import.
HIDDEN_IMPORTS_BLOCK = [
    'PyQt6.sip',
    'docx',
    'playwright.sync_api',
    'google.genai',
    'google.genai.types',
    'qoder_agent_sdk',
    # api/handlers и api/servers ленивые (PEP 562, importlib) —
    # PyInstaller не видит эти импорты при анализе.
    *LAZY_HANDLER_HIDDEN_IMPORTS,
    *LAZY_SERVER_HIDDEN_IMPORTS,
]
MANUAL_COLLECT_DATA_MODULES = {'certifi', 'docx', 'qoder_agent_sdk'}
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
    'qoder_agent_sdk': 'qoder-agent-sdk',
    'recognizers_text': 'recognizers-text',
    'recognizers_number': 'recognizers-text-number',
}

ESSENTIAL_PACKAGES = {
    'cryptography',
    'defusedxml',
    'idna',
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
    'soupsieve',
    'urllib3',
}
FORCED_VERSIONS = {
    'cryptography': '>=48.0.1',
    'defusedxml': '>=0.7.1',
    'idna': '>=3.15',
    'pydantic': '>=2.0.0',
    'qoder-agent-sdk': '>=1.0.8',
    'setuptools': '<81',
    'soupsieve': '>=2.8.4',
    'urllib3': '>=2.7.0',
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

    configured_browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    localappdata = os.environ.get("LOCALAPPDATA")
    browser_cache_candidates = []
    if configured_browser_cache and configured_browser_cache != "0":
        browser_cache_candidates.append(Path(configured_browser_cache))
    if localappdata:
        browser_cache_candidates.append(Path(localappdata) / "ms-playwright")

    if not project_browser_cache.exists():
        browser_cache = next(
            (candidate for candidate in browser_cache_candidates if candidate.exists()),
            None,
        )
        if browser_cache is not None:
            discovered.append(
                (browser_cache, Path("playwright_runtime") / "ms-playwright")
            )
        else:
            print(
                "     [WARN] Cache браузеров Playwright не найден, "
                "bundled browser cache будет пропущен."
            )

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
    entries = []
    for src, dst in ADDITIONAL_DATA:
        if Path(src).exists() or (PROJECT_ROOT / src).exists():
            entries.append((src, dst))
        else:
            print(f"     [WARN] Файл или папка '{src}' не найдены, пропуск.")
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
    
    try:
        standard_libs = set(sys.stdlib_module_names)
        print(f"  -> Используется полный список стандартных библиотек Python {sys.version.split()[0]}.")
    except AttributeError:
        standard_libs = set(sys.builtin_module_names)
        print(f"  -> [WARN] Используется базовый список встроенных модулей. Точность может быть ниже.")

    for module_name in sorted(list(imports)):
        if module_name in PROJECT_MODULES or module_name in standard_libs:
            continue
            
        if (PROJECT_ROOT / f"{module_name}.py").exists() or (PROJECT_ROOT / module_name).is_dir():
            continue
            
        if module_name in ('AppKit', 'objc'):
            continue
            
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

    collect_data_modules = set(collect_data_flags)
    collect_data_modules.update(MANUAL_COLLECT_DATA_MODULES)

    collect_data_args = [
        f'--collect-data="{module}"' for module in sorted(collect_data_modules)
    ]

    def build_runner_command(mode):
        runner_args = [
            f'"%PYTHON_CMD%" build_runner.py --mode {mode}',
            '--name="%AppName%"',
            *collect_data_args,
        ]
        return " ^\n".join(runner_args)

    pyinstaller_command_full_portable = build_runner_command("portable")
    pyinstaller_command_hybrid = build_runner_command("hybrid")
    pyinstaller_command_advanced = build_runner_command("advanced")
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
    echo [!!!] Не найден интерпретатор Python: "%PYTHON_CMD%"
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
echo   4. ИНСТАЛЛЯТОР (Inno Setup)
echo      - Собирает Продвинутую версию, затем создает установщик Setup.exe.
echo      - Требует установленного Inno Setup.
echo.
echo   5. Назад в главное меню
echo.
echo ======================================================
set /p build_choice="Выберите действие (1, 2, 3, 4 или 5): "

if not defined build_choice ( goto build_menu )
if "%build_choice%"=="1" ( goto build_full_portable )
if "%build_choice%"=="2" ( goto build_hybrid )
if "%build_choice%"=="3" ( goto build_advanced )
if "%build_choice%"=="4" ( goto build_installer )
if "%build_choice%"=="5" ( goto menu )

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
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
{pyinstaller_command_advanced}
call :build_app_end
goto :eof


:: --- Блок сборки: ИНСТАЛЛЯТОР ---
:build_installer
echo [+] Проверка наличия Inno Setup...
set "ISCC_CMD="
for /f "delims=" %%I in ('where iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
if not defined ISCC_CMD (
    for /f "delims=" %%I in ('where /R "%PROGRAMFILES(X86)%" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
)
if not defined ISCC_CMD (
    for /f "delims=" %%I in ('where /R "%PROGRAMFILES%" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
)
if not defined ISCC_CMD (
    if exist "%LOCALAPPDATA%\\Programs" (
        for /f "delims=" %%I in ('where /R "%LOCALAPPDATA%\\Programs" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
    )
)
if not defined ISCC_CMD (
    for %%D in ("C:\\Program Files (x86)\\Inno Setup 8" "C:\\Program Files (x86)\\Inno Setup 7" "C:\\Program Files (x86)\\Inno Setup 6" "C:\\Program Files\\Inno Setup 6" "C:\\Program Files (x86)\\Inno Setup 5" "C:\\Program Files\\Inno Setup 5" "C:\\Program Files (x86)\\Inno Setup" "C:\\Program Files\\Inno Setup" "%LOCALAPPDATA%\\Programs\\Inno Setup 8" "%LOCALAPPDATA%\\Programs\\Inno Setup 7" "%LOCALAPPDATA%\\Programs\\Inno Setup 6" "%LOCALAPPDATA%\\Programs\\Inno Setup 5" "%LOCALAPPDATA%\\Programs\\Inno Setup") do (
        if exist "%%~D\\ISCC.exe" set "ISCC_CMD=%%~D\\ISCC.exe"
    )
)

if not defined ISCC_CMD (
    echo [+] Inno Setup не найден. Попытка автоматической установки через winget...
    where winget >nul 2^>^&1
    if errorlevel 1 (
        echo [!!!] winget не найден. Установите Inno Setup вручную: https://jrsoftware.org/isdl.php
        pause
        goto :eof
    )
    winget install --id JRSoftware.InnoSetup -e --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 (
        echo [!!!] winget не смог установить Inno Setup.
        echo     Проверьте сеть, winget/App Installer или установите вручную.
        pause
        goto :eof
    )
    
    timeout /t 3 /nobreak >nul
    
    for /f "delims=" %%I in ('where iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
    if not defined ISCC_CMD (
        for /f "delims=" %%I in ('where /R "%PROGRAMFILES(X86)%" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
    )
    if not defined ISCC_CMD (
        for /f "delims=" %%I in ('where /R "%PROGRAMFILES%" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
    )
    if not defined ISCC_CMD (
        if exist "%LOCALAPPDATA%\\Programs" (
            for /f "delims=" %%I in ('where /R "%LOCALAPPDATA%\\Programs" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
        )
    )
    if not defined ISCC_CMD (
        for %%D in ("C:\\Program Files (x86)\\Inno Setup 8" "C:\\Program Files (x86)\\Inno Setup 7" "C:\\Program Files (x86)\\Inno Setup 6" "C:\\Program Files\\Inno Setup 6" "C:\\Program Files (x86)\\Inno Setup 5" "C:\\Program Files\\Inno Setup 5" "C:\\Program Files (x86)\\Inno Setup" "C:\\Program Files\\Inno Setup" "%LOCALAPPDATA%\\Programs\\Inno Setup 8" "%LOCALAPPDATA%\\Programs\\Inno Setup 7" "%LOCALAPPDATA%\\Programs\\Inno Setup 6" "%LOCALAPPDATA%\\Programs\\Inno Setup 5" "%LOCALAPPDATA%\\Programs\\Inno Setup") do (
            if exist "%%~D\\ISCC.exe" set "ISCC_CMD=%%~D\\ISCC.exe"
        )
    )
)

if not defined ISCC_CMD (
    echo [!!!] Не удалось автоматически установить Inno Setup. Установите его вручную: https://jrsoftware.org/isdl.php
    pause
    goto :eof
)

call :build_app_base "ИНСТАЛЛЯТОР"
{pyinstaller_command_advanced}
if %ERRORLEVEL% NEQ 0 (
    call :build_app_end
    goto :eof
)
echo.
echo [+] Создание инсталлятора через Inno Setup...
"%ISCC_CMD%" "/DAppBuildName=%AppName%" windows_installer.iss
if errorlevel 1 (
    echo [!!!] Ошибка при создании инсталлятора.
) else (
    echo [OK] Инсталлятор успешно создан в папке installer_output.
    echo [+] Очистка временных файлов сборки...
    if exist "dist\%AppName%" rmdir /S /Q "dist\%AppName%"
    set "BUILD_INSTALLER_SUCCESS=1"
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
    if defined BUILD_INSTALLER_SUCCESS (
        echo     Готовый установщик находится в папке 'installer_output'.
    ) else (
        echo     Готовое приложение находится в папке 'dist'.
    )
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
