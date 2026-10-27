@echo off
chcp 65001 >nul
setlocal EnableExtensions
title AIOS - платформа управления инновациями
rem ================================================================
rem  Запуск AIOS на Windows в один клик: дважды щёлкните этот файл.
rem  Первый запуск ставит зависимости (5-10 минут, нужен интернет),
rem  следующие — сразу открывают платформу. Окно не закрывайте:
rem  пока оно открыто, платформа работает. Остановить — закрыть окно.
rem ================================================================
cd /d "%~dp0"

set "PORT=8000"
set "URL=http://localhost:%PORT%"

rem --- платформа уже запущена (второй двойной клик) — просто открываем браузер
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
    echo Платформа уже запущена - открываю браузер.
    start "" "%URL%"
    timeout /t 3 >nul
    exit /b 0
)

echo.
echo  AIOS - платформа управления инновациями университета
echo  ----------------------------------------------------
echo.

rem --- 1. Python 3.10 или новее
call :find_python
if defined PY goto :have_python

echo Python 3.10+ не найден на компьютере.
where winget >nul 2>nul
if errorlevel 1 goto :no_python
echo Устанавливаю Python 3.12 - это займёт пару минут...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto :have_python

:no_python
echo.
echo Не удалось найти или установить Python автоматически.
echo Сейчас откроется страница загрузки Python. Скачайте установщик,
echo при установке ОБЯЗАТЕЛЬНО отметьте галочку "Add python.exe to PATH",
echo затем снова дважды щёлкните start_windows.bat.
start "" "https://www.python.org/downloads/windows/"
goto :fail

:have_python
echo [1/4] Python найден: %PY%

rem --- 2. Виртуальное окружение venv
if exist "venv\Scripts\python.exe" goto :venv_ready
echo [2/4] Создаю виртуальное окружение venv...
%PY% -m venv venv
if errorlevel 1 goto :fail
:venv_ready
call "venv\Scripts\activate.bat"
rem errorlevel после activate.bat ненадёжен - проверяем, что окружение включилось
if not defined VIRTUAL_ENV goto :fail

rem --- 3. Зависимости: requirements.txt (если файла нет - создаём базовый)
if not exist "requirements.txt" call :write_requirements
fc /b "requirements.txt" "venv\.requirements.installed" >nul 2>nul
if not errorlevel 1 goto :deps_ready
echo [3/4] Устанавливаю зависимости - при первом запуске 5-10 минут...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
copy /y "requirements.txt" "venv\.requirements.installed" >nul
:deps_ready
echo [3/4] Зависимости установлены.

rem --- настройки: .env из шаблона (стартовый администратор)
if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

rem --- 4. Запуск; браузер откроется сам, когда сервер ответит
echo [4/4] Запускаю платформу: %URL%
echo.
echo  Вход администратора: admin@university.local / admin12345
echo  НЕ ЗАКРЫВАЙТЕ это окно, пока работаете с платформой.
echo  Чтобы остановить платформу - закройте окно.
echo.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 180; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/' | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"
python -m uvicorn api.main:app --host 127.0.0.1 --port %PORT%
echo.
echo Платформа остановлена.
pause
exit /b 0

:fail
echo.
echo  Что-то пошло не так. Сфотографируйте это окно и покажите организаторам.
pause
exit /b 1

rem ---------------------------------------------------------------
:find_python
set "PY="
rem предпочитаем проверенные версии: для них есть готовые пакеты всех зависимостей
for %%V in (3.12 3.13 3.11 3.10) do (
    py -%%V -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -%%V"
        goto :eof
    )
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3" & goto :eof
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python" & goto :eof
rem сразу после установки через winget PATH в этом окне ещё старый
for %%V in (313 312 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe""
        goto :eof
    )
)
goto :eof

:write_requirements
echo requirements.txt не найден - создаю базовый список зависимостей.
> "requirements.txt" (
    echo fastapi^>=0.110
    echo uvicorn[standard]^>=0.29
    echo pydantic^>=2.6
    echo httpx^>=0.27
    echo python-multipart^>=0.0.9
    echo pypdf^>=4.0
    echo qdrant-client
    echo openpyxl
    echo python-docx
    echo pyyaml
    echo tzdata
    echo pillow
)
goto :eof
