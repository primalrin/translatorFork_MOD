@echo off
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
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~f0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    cscript "%temp%\getadmin.vbs" & exit /B
)
if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )

:: --- Этап 2: Настройка рабочего окружения ---
:setup_env
cd /d "%~dp0"
if exist "%cd%\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=%cd%\.venv\Scripts\python.exe"
) else if exist "%cd%\venv\Scripts\python.exe" (
    set "PYTHON_CMD=%cd%\venv\Scripts\python.exe"
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
echo [+] Запуск установки из файла 'requirements.txt'...
"%PYTHON_CMD%" -m pip install --upgrade -r "requirements.txt"
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
"%PYTHON_CMD%" build_runner.py --mode portable ^
--name="%AppName%" ^
--collect-data="PyQt6" ^
--collect-data="certifi" ^
--collect-data="docx" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="qoder_agent_sdk" ^
--collect-data="werkzeug"
call :build_app_end
goto :eof


:: --- Блок сборки: ГИБРИДНАЯ ---
:build_hybrid
call :build_app_base "ГИБРИДНАЯ"
"%PYTHON_CMD%" build_runner.py --mode hybrid ^
--name="%AppName%" ^
--collect-data="PyQt6" ^
--collect-data="certifi" ^
--collect-data="docx" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="qoder_agent_sdk" ^
--collect-data="werkzeug"
call :build_app_end
goto :eof


:: --- Блок сборки: ПРОДВИНУТАЯ ---
:build_advanced
call :build_app_base "ПРОДВИНУТАЯ"
"%PYTHON_CMD%" build_runner.py --mode advanced ^
--name="%AppName%" ^
--collect-data="PyQt6" ^
--collect-data="certifi" ^
--collect-data="docx" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="qoder_agent_sdk" ^
--collect-data="werkzeug"
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
    if exist "%LOCALAPPDATA%\Programs" (
        for /f "delims=" %%I in ('where /R "%LOCALAPPDATA%\Programs" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
    )
)
if not defined ISCC_CMD (
    for %%D in ("C:\Program Files (x86)\Inno Setup 8" "C:\Program Files (x86)\Inno Setup 7" "C:\Program Files (x86)\Inno Setup 6" "C:\Program Files\Inno Setup 6" "C:\Program Files (x86)\Inno Setup 5" "C:\Program Files\Inno Setup 5" "C:\Program Files (x86)\Inno Setup" "C:\Program Files\Inno Setup" "%LOCALAPPDATA%\Programs\Inno Setup 8" "%LOCALAPPDATA%\Programs\Inno Setup 7" "%LOCALAPPDATA%\Programs\Inno Setup 6" "%LOCALAPPDATA%\Programs\Inno Setup 5" "%LOCALAPPDATA%\Programs\Inno Setup") do (
        if exist "%%~D\ISCC.exe" set "ISCC_CMD=%%~D\ISCC.exe"
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
        if exist "%LOCALAPPDATA%\Programs" (
            for /f "delims=" %%I in ('where /R "%LOCALAPPDATA%\Programs" iscc.exe 2^>nul') do set "ISCC_CMD=%%I"
        )
    )
    if not defined ISCC_CMD (
        for %%D in ("C:\Program Files (x86)\Inno Setup 8" "C:\Program Files (x86)\Inno Setup 7" "C:\Program Files (x86)\Inno Setup 6" "C:\Program Files\Inno Setup 6" "C:\Program Files (x86)\Inno Setup 5" "C:\Program Files\Inno Setup 5" "C:\Program Files (x86)\Inno Setup" "C:\Program Files\Inno Setup" "%LOCALAPPDATA%\Programs\Inno Setup 8" "%LOCALAPPDATA%\Programs\Inno Setup 7" "%LOCALAPPDATA%\Programs\Inno Setup 6" "%LOCALAPPDATA%\Programs\Inno Setup 5" "%LOCALAPPDATA%\Programs\Inno Setup") do (
            if exist "%%~D\ISCC.exe" set "ISCC_CMD=%%~D\ISCC.exe"
        )
    )
)

if not defined ISCC_CMD (
    echo [!!!] Не удалось автоматически установить Inno Setup. Установите его вручную: https://jrsoftware.org/isdl.php
    pause
    goto :eof
)

call :build_app_base "ИНСТАЛЛЯТОР"
"%PYTHON_CMD%" build_runner.py --mode advanced ^
--name="%AppName%" ^
--collect-data="PyQt6" ^
--collect-data="certifi" ^
--collect-data="docx" ^
--collect-data="emoji" ^
--collect-data="jieba" ^
--collect-data="lxml" ^
--collect-data="qoder_agent_sdk" ^
--collect-data="werkzeug"
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
"%PYTHON_CMD%" -m pip install --upgrade -r "requirements.txt" pyinstaller pyinstaller-hooks-contrib
if %ERRORLEVEL% NEQ 0 (
    echo [!!!] Ошибка при установке зависимостей. Проверьте подключение к интернету.
    pause
    goto menu
)
if exist "dist\chatgpt-profile-run" rmdir /S /Q "dist\chatgpt-profile-run"
if exist "dist\logs" rmdir /S /Q "dist\logs"
if exist "dist\%AppName%\chatgpt-profile-run" rmdir /S /Q "dist\%AppName%\chatgpt-profile-run"
if exist "dist\%AppName%\logs" rmdir /S /Q "dist\%AppName%\logs"
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

