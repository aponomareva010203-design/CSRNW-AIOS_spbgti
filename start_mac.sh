#!/bin/bash
# ================================================================
#  Запуск AIOS на macOS в один клик: дважды щёлкните start_mac.command
#  (он вызывает этот скрипт в Терминале). Первый запуск ставит
#  зависимости (5-10 минут, нужен интернет), следующие — сразу
#  открывают платформу. Окно Терминала не закрывайте: пока оно
#  открыто, платформа работает. Остановить — закрыть окно или Ctrl+C.
# ================================================================

cd "$(dirname "$0")" || exit 1

PORT=8000
URL="http://localhost:$PORT"

pause_exit() {
    echo
    read -r -p "Нажмите Enter, чтобы закрыть окно..." _
    exit "${1:-0}"
}

fail() {
    echo
    echo " Что-то пошло не так. Сфотографируйте это окно и покажите организаторам."
    pause_exit 1
}

open_url() {
    if command -v open >/dev/null 2>&1; then open "$1"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
    fi
}

is_up() { curl -fs -o /dev/null --max-time 2 "$URL/"; }

# --- платформа уже запущена (второй двойной клик) — просто открываем браузер
if is_up; then
    echo "Платформа уже запущена — открываю браузер."
    open_url "$URL"
    exit 0
fi

echo
echo " AIOS — платформа управления инновациями университета"
echo " ----------------------------------------------------"
echo

# --- 1. Python 3.10 или новее (проверенные версии — первыми)
find_python() {
    PY=""
    for c in python3.12 python3.13 python3.11 python3.10 python3 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
             /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if command -v "$c" >/dev/null 2>&1 &&
           "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PY="$c"
            return 0
        fi
    done
    return 1
}

if ! find_python; then
    echo "Python 3.10+ не найден на компьютере."
    if command -v brew >/dev/null 2>&1; then
        echo "Устанавливаю Python 3.12 через Homebrew — это займёт пару минут..."
        brew install python@3.12 && find_python
    fi
fi
if [ -z "$PY" ]; then
    echo
    echo "Не удалось найти или установить Python автоматически."
    echo "Сейчас откроется страница загрузки Python: скачайте установщик"
    echo "для macOS, установите его и снова дважды щёлкните start_mac.command."
    open_url "https://www.python.org/downloads/macos/"
    pause_exit 1
fi
echo "[1/4] Python найден: $PY ($("$PY" --version 2>&1))"

# --- 2. Виртуальное окружение venv
if [ ! -x "venv/bin/python" ]; then
    echo "[2/4] Создаю виртуальное окружение venv..."
    "$PY" -m venv venv || fail
fi
# shellcheck disable=SC1091
source "venv/bin/activate" || fail

# --- 3. Зависимости: requirements.txt (если файла нет — создаём базовый)
if [ ! -f "requirements.txt" ]; then
    echo "requirements.txt не найден — создаю базовый список зависимостей."
    cat > requirements.txt <<'REQ'
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
httpx>=0.27
python-multipart>=0.0.9
pypdf>=4.0
qdrant-client
openpyxl
python-docx
pyyaml
pillow
REQ
fi
if ! cmp -s "requirements.txt" "venv/.requirements.installed"; then
    echo "[3/4] Устанавливаю зависимости — при первом запуске 5-10 минут..."
    python -m pip install --upgrade pip || fail
    python -m pip install -r requirements.txt || fail
    cp "requirements.txt" "venv/.requirements.installed"
fi
echo "[3/4] Зависимости установлены."

# --- настройки: .env из шаблона (стартовый администратор)
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp ".env.example" ".env"
fi

# --- 4. Запуск; браузер откроется сам, когда сервер ответит
echo "[4/4] Запускаю платформу: $URL"
echo
echo " Вход администратора: admin@university.local / admin12345"
echo " НЕ ЗАКРЫВАЙТЕ это окно, пока работаете с платформой."
echo " Чтобы остановить платформу — закройте окно или нажмите Ctrl+C."
echo

(
    for _ in $(seq 1 180); do
        if is_up; then open_url "$URL"; break; fi
        sleep 1
    done
) &
WAITER=$!

trap 'kill "$WAITER" 2>/dev/null' EXIT
python -m uvicorn api.main:app --host 127.0.0.1 --port "$PORT"
echo
echo "Платформа остановлена."
pause_exit 0
