"""Песочница исполнения расчётного кода (api/runtime/compute_sandbox.py).

Инструмент run_calc — единственная возможность платформы, которая превращает
агента из «чат-бота поверх своих PDF» в инженерного помощника: модель пишет
короткий расчётный скрипт, платформа исполняет его и возвращает результат
вместе с исходным кодом (агент показывает работу, инженер её проверяет).

Изоляция — независимые рубежи, по убыванию строгости:

  1. Отделение от файлов платформы. Способ выбирается при первом расчёте
     автоматически (_isolation_mode), потому что доступное зависит от машины:
       • bubblewrap — отдельные namespace'ы, сети нет, ФС только на чтение.
         Строже всего, но требует непривилегированных user namespace'ов;
         на Ubuntu 24.04 они по умолчанию запрещены политикой AppArmor
         (kernel.apparmor_restrict_unprivileged_userns=1), и тогда bwrap
         не работает вовсе — это проверяется пробным запуском, а не
         наличием бинарника.
       • отдельный пользователь (sudo -u): расчёт исполняется от учётной
         записи без прав на файлы владельца платформы. Домашний каталог
         владельца обычно закрыт (drwxr-x---), поэтому .env, .aios-runtime
         и код платформы недостижимы по обычным правам ядра — без экзотики
         и без ослабления системных настроек.
       • прямой запуск — если ни то, ни другое не доступно.
  2. Отдельный интерпретатор /opt/aios-sandbox/venv: расчётные библиотеки
     (numpy/scipy/rdkit/thermo/…) не попадают в venv боевого API и не могут
     сломать его зависимости. Ставится и обновляется независимо.
  3. Лимиты и таймаут в самом дочернем процессе: RLIMIT_AS (виртуальная
     память), RLIMIT_CPU (процессорное время), RLIMIT_FSIZE (размер файла),
     плюс wall-таймаут снаружи с убийством всей группы процессов.
  4. Блокировка сокетов внутри интерпретатора: за внешними данными ходят
     инструменты платформы, у которых есть лимиты и журналирование.

Честная граница: при прямом запуске (рубеж 1 недоступен) песочница защищает
от бесконечного цикла, утечки памяти, разрастания файлов и случайной порчи
данных, но не от целенаправленной атаки — код исполняется тем же
пользователем. Фактический режим виден в isolation_report() и должен
попадать в эксплуатационную документацию, а не умалчиваться.

Ресурсы сервера (4 vCPU, 7.8 ГБ RAM, без swap): одновременно исполняется
не больше MAX_PARALLEL расчётов — семафор, а не очередь запросов, чтобы
OOM-killer не добрался до боевого uvicorn. Пик: 2 × ~700 МБ поверх ~1 ГБ
базового фона.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
from functools import lru_cache
from pathlib import Path

# Интерпретатор песочницы — отдельный venv, не тот, в котором работает API.
SANDBOX_PYTHON = os.environ.get(
    "AIOS_SANDBOX_PYTHON", "/opt/aios-sandbox/venv/bin/python"
)
SANDBOX_ROOT = Path(SANDBOX_PYTHON).parent.parent  # /opt/aios-sandbox/venv

# Учётная запись, от которой исполняется расчёт, когда bubblewrap недоступен.
# Домашний каталог владельца платформы обычно закрыт (drwxr-x---), поэтому
# посторонний пользователь не дотянется ни до .env, ни до рабочих данных —
# обычными правами ядра, без ослабления системных настроек.
SANDBOX_USER = os.environ.get("AIOS_SANDBOX_USER", "aios-sandbox")

MEM_LIMIT_BYTES = 2 * 1024 * 1024 * 1024   # RLIMIT_AS: виртуальная память, не RSS
CPU_SECONDS = 20                            # процессорное время
WALL_TIMEOUT = 30                           # общее время, включая старт интерпретатора
FSIZE_LIMIT = 16 * 1024 * 1024              # максимальный размер создаваемого файла
MAX_STDOUT_CHARS = 20_000                   # сколько текста отдаём модели
MAX_ARTIFACTS = 4                           # сколько картинок/файлов забираем из расчёта
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_PARALLEL = 2
# Сколько ждать освободившегося слота. Замеры: типичный инженерный расчёт
# 0,1-0,6 с (запуск интерпретатора 21 мс, импорт numpy 48 мс, scipy 220 мс,
# 300 подгонок кривой 260 мс), очередь из 17 одновременных запросов
# расходится меньше чем за секунду. Ждать дольше приходится, только если
# два расчёта зависли до полного таймаута; тогда честный отказ лучше
# молчаливого ожидания, при котором участник смотрит в пустой экран.
QUEUE_TIMEOUT = 25

# Профили лимитов: (процессорное время, общее время) в секундах.
#   calc  — расчёт агента в чате (run_calc);
#   model — прогноз, график и подбор режима моделью коллектива: модель
#           пересобирается из своих данных, подбор режима перебирает тысячи
#           вариантов;
#   train — обучение модели с перекрёстной проверкой и кривой обучения.
# Обучение идёт по одному (отдельный семафор) и занимает один из общих
# слотов — второй всегда остаётся для расчётов в чате.
PROFILES = {
    "model": (50, 60),
    "train": (170, 180),
}


def _limits(profile: str) -> tuple[int, int]:
    """Лимиты профиля; calc читается в момент вызова (тесты его сужают)."""
    if profile in PROFILES:
        return PROFILES[profile]
    return CPU_SECONDS, WALL_TIMEOUT
TRAIN_QUEUE_TIMEOUT = 120

_semaphore: asyncio.Semaphore | None = None
_train_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Семафор создаётся лениво — привязка к работающему циклу событий."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_PARALLEL)
    return _semaphore


def _get_train_semaphore() -> asyncio.Semaphore:
    global _train_semaphore
    if _train_semaphore is None:
        _train_semaphore = asyncio.Semaphore(1)
    return _train_semaphore


def sandbox_available() -> bool:
    """Установлен ли интерпретатор песочницы (проверка перед регистрацией
    инструмента — без венва run_calc не должен появляться в реестре)."""
    return Path(SANDBOX_PYTHON).is_file()


# ---------------------------------------------------------------------------
# Обвязка, исполняемая внутри песочницы

_RUNNER = textwrap.dedent(
    '''
    import base64, io, json, os, resource, sys, traceback

    # Рубеж 3: лимиты ставим до импорта тяжёлых библиотек. Жёсткий лимит
    # уже может быть ниже желаемого (настройка systemd, ulimit в окружении) —
    # тогда setrlimit с большим значением упал бы с ValueError и расчёт не
    # стартовал бы вовсе; берём минимум из желаемого и разрешённого.
    def _limit(what, want):
        try:
            soft, hard = resource.getrlimit(what)
        except (ValueError, OSError):
            return
        value = want if hard == resource.RLIM_INFINITY else min(want, hard)
        try:
            resource.setrlimit(what, (value, hard))
        except (ValueError, OSError):
            pass

    _limit(resource.RLIMIT_AS, {mem})
    _limit(resource.RLIMIT_CPU, {cpu})
    _limit(resource.RLIMIT_FSIZE, {fsize})
    _limit(resource.RLIMIT_CORE, 0)

    # Сеть из расчётного кода недоступна: за внешними данными ходят
    # отдельные инструменты (literature_search, compound_lookup), у которых
    # есть свои лимиты и логирование. Дублирующий рубеж к bubblewrap.
    #
    # Закрывается СОЕДИНЕНИЕ, а не класс сокета. Раньше socket.socket
    # подменялся функцией — и тогда стандартный ssl.py падал на
    # «class SSLSocket(socket)» с TypeError: function() argument 'code'
    # must be code, not str. Через ssl импортируется asyncio, через него
    # joblib, а через joblib — scikit-learn и statsmodels: ни одна модель
    # по данным участника в песочнице не собиралась вовсе, и методики
    # podgonka_modeli и plan_eksperimenta падали на первой же строке.
    # Подкласс настоящего сокета наследование не ломает: ssl, asyncio и
    # multiprocessing импортируются как обычно, а connect закрыт.
    import socket
    _NET_CLOSED = ("Сеть из расчёта недоступна. За внешними данными "
                   "используйте инструменты платформы.")

    class _OfflineSocket(socket.socket):
        def connect(self, *a, **k):
            raise OSError(_NET_CLOSED)

        def connect_ex(self, *a, **k):
            raise OSError(_NET_CLOSED)

        def sendto(self, *a, **k):
            raise OSError(_NET_CLOSED)

    def _no_network(*a, **k):
        raise OSError(_NET_CLOSED)

    socket.socket = _OfflineSocket
    socket.create_connection = _no_network
    socket.getaddrinfo = _no_network

    # Графики — только в файл, интерактивный бэкенд в headless-среде падает.
    os.environ.setdefault("MPLBACKEND", "Agg")

    _stdout = io.StringIO()
    _payload = {{"ok": True, "stdout": "", "result": None, "artifacts": [], "error": None}}

    # Что лежало в рабочем каталоге до расчёта — это входные данные
    # (скрипт, обвязка, выгрузки участника). Артефактами считаем только
    # то, что расчёт создал сам, иначе входной xlsx вернулся бы обратно.
    _before = set(os.listdir("."))

    try:
        with open("main.py", "r", encoding="utf-8") as fh:
            _source = fh.read()
        _ns = {{"__name__": "__main__"}}
        _real_stdout = sys.stdout
        sys.stdout = _stdout
        try:
            exec(compile(_source, "main.py", "exec"), _ns)
        finally:
            sys.stdout = _real_stdout

        # Соглашение: переменная result — то, что агент показывает человеку.
        _value = _ns.get("result", None)
        try:
            json.dumps(_value)
            _payload["result"] = _value
        except (TypeError, ValueError):
            _payload["result"] = repr(_value)[:4000]

    except Exception:
        _payload["ok"] = False
        _payload["error"] = traceback.format_exc(limit=4)[-4000:]

    _payload["stdout"] = _stdout.getvalue()[:{max_out}]

    # Файлы, созданные расчётом (графики, выгрузки) — забираем в ответ.
    for _name in sorted(os.listdir(".")):
        if _name in _before or len(_payload["artifacts"]) >= {max_art}:
            continue
        try:
            _size = os.path.getsize(_name)
        except OSError:
            continue
        if _size > {max_art_bytes}:
            continue
        _ext = _name.rsplit(".", 1)[-1].lower()
        # svg сознательно не забираем: векторные графики идут через
        # chart_generate, у которого своя санитизация перед вставкой в DOM
        if _ext in ("png", "csv", "txt", "md", "xlsx", "docx"):
            with open(_name, "rb") as fh:
                _payload["artifacts"].append({{
                    "name": _name,
                    "kind": _ext,
                    "base64": base64.b64encode(fh.read()).decode("ascii"),
                }})

    sys.stderr.write("\\n__AIOS_RESULT__" + json.dumps(_payload, ensure_ascii=False))
    '''
)


def _bwrap_prefix(workdir: Path) -> list[str]:
    return [
        "bwrap",
        "--unshare-all",              # свои namespace'ы, в том числе сетевой
        "--die-with-parent",
        "--new-session",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind", str(SANDBOX_ROOT), str(SANDBOX_ROOT),
        "--bind", str(workdir), "/work",
        "--chdir", "/work",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--setenv", "HOME", "/work",
        "--setenv", "MPLBACKEND", "Agg",
        "--",
    ]


@lru_cache(maxsize=1)
def _isolation_mode() -> str:
    """Какой способ изоляции реально работает на этой машине: bwrap | user | none.

    Проверяется пробным запуском, а не наличием бинарника: на Ubuntu 24.04
    bubblewrap установлен и исполняется, но создать user namespace не может
    (политика AppArmor), и падает с «setting up uid map: Permission denied».
    Определять по `which bwrap` значило бы уверенно считать изоляцию
    работающей там, где её нет.
    """
    probe = ["-c", "print('ok')"]

    if shutil.which("bwrap"):
        with tempfile.TemporaryDirectory(prefix="aios-probe-") as tmp:
            try:
                done = subprocess.run(
                    [*_bwrap_prefix(Path(tmp)), SANDBOX_PYTHON, "-I", *probe],
                    capture_output=True, timeout=20, check=False,
                )
                if done.returncode == 0 and b"ok" in done.stdout:
                    return "bwrap"
            except (OSError, subprocess.SubprocessError):
                pass

    if SANDBOX_USER:
        try:
            done = subprocess.run(
                ["sudo", "-n", "-u", SANDBOX_USER, SANDBOX_PYTHON, "-I", *probe],
                capture_output=True, timeout=20, check=False,
            )
            if done.returncode == 0 and b"ok" in done.stdout:
                return "user"
        except (OSError, subprocess.SubprocessError):
            pass

    return "none"


def isolation_report() -> dict:
    """Фактический режим изоляции — для страницы администратора и для
    эксплуатационной документации: режим должен быть виден, а не подразумеваться."""
    mode = _isolation_mode() if sandbox_available() else "unavailable"
    return {
        "mode": mode,
        "описание": {
            "bwrap": "отдельные namespace'ы: сети нет, файлы платформы недостижимы",
            "user": f"отдельный пользователь {SANDBOX_USER}: файлы платформы "
                    "недостижимы по правам доступа, сеть блокируется в интерпретаторе",
            "none": "общий пользователь: защита от зависаний и утечек памяти есть, "
                    "от целенаправленного чтения файлов — нет",
            "unavailable": "песочница не установлена, run_calc недоступен",
        }[mode],
        "лимиты": {"память_ГБ": round(MEM_LIMIT_BYTES / 2**30, 1),
                   "процессор_с": CPU_SECONDS, "общий_таймаут_с": WALL_TIMEOUT,
                   "одновременно": MAX_PARALLEL},
    }


def _build_command(workdir: Path) -> list[str]:
    """Команда запуска с доступным на этой машине способом изоляции.

    -I: изолированный режим — игнорируются PYTHONPATH и пользовательские
    site-packages, окружение платформы в расчёт не протекает (проверено).
    Флага -S здесь быть НЕ должно: он отключает загрузку site целиком, и
    тогда невидимы сами расчётные библиотеки venv песочницы (numpy, scipy,
    rdkit, thermo) — ради которых песочница и заводится.
    """
    # -B: не писать байткод. Режим -I игнорирует PYTHONDONTWRITEBYTECODE, а .pyc
    # от пользователя песочницы платформа удалить не может (PermissionError
    # при уборке рабочей папки), когда расчёт импортирует файл из неё (model_kernel).
    inner = [SANDBOX_PYTHON, "-B", "-I", "_runner.py"]
    mode = _isolation_mode()
    if mode == "bwrap":
        return [*_bwrap_prefix(workdir), *inner]
    if mode == "user":
        # sudo -n: без пароля; правило в sudoers разрешает csrnw запускать
        # ровно этот интерпретатор от имени пользователя песочницы.
        return ["sudo", "-n", "-u", SANDBOX_USER, *inner]
    return inner


async def run_code(code: str, files: dict[str, bytes] | None = None, profile: str = "calc") -> dict:
    """Исполняет расчётный код, возвращает результат для модели и интерфейса.

    files — входные данные расчёта (например, выгруженный участником xlsx,
    подготовленный инструментом read_data_file): имя → содержимое, кладутся
    рядом со скриптом, открываются обычным open()/pandas.read_excel().

    profile — набор лимитов из PROFILES: calc (расчёт в чате), model
    (прогноз моделью коллектива), train (обучение модели, по одному).

    Возврат:
        {ok, stdout, result, artifacts:[{name,kind,base64}], error, code}
    Поле code возвращается всегда — интерфейс показывает исполненный код
    в компоненте calc_trace, чтобы инженер мог проверить расчёт.
    """
    cpu_limit, wall_limit = _limits(profile)
    train_semaphore = _get_train_semaphore() if profile == "train" else None
    if train_semaphore is not None:
        try:
            await asyncio.wait_for(train_semaphore.acquire(), timeout=TRAIN_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "Сейчас обучается другая модель, очередь не освободилась за "
                         f"{TRAIN_QUEUE_TIMEOUT} с. Повторите через минуту.",
                "code": code,
            }
    try:
        return await _run_code(code, files, cpu_limit, wall_limit)
    finally:
        if train_semaphore is not None:
            train_semaphore.release()


async def _run_code(code: str, files: dict[str, bytes] | None, cpu_limit: int, wall_limit: int) -> dict:
    if not sandbox_available():
        return {
            "ok": False,
            "error": "Расчётная песочница не установлена на этом сервере "
                     "(нет интерпретатора). Сообщите администратору платформы.",
            "code": code,
        }
    if not str(code).strip():
        return {"ok": False, "error": "Пустой код расчёта", "code": code}

    # Слот в очереди берём с ограничением по времени: без него запрос,
    # пришедший в момент, когда два расчёта зависли, ждал бы молча, а
    # участник смотрел бы в пустой экран до обрыва соединения.
    semaphore = _get_semaphore()
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": "Сейчас выполняются другие расчёты, слот не освободился "
                     f"за {QUEUE_TIMEOUT} с. Повторите запрос через несколько секунд.",
            "code": code,
        }
    try:
        with tempfile.TemporaryDirectory(prefix="aios-calc-", ignore_cleanup_errors=True) as tmp:
            workdir = Path(tmp)
            if _isolation_mode() == "user":
                # Расчёт идёт от другой учётной записи — ей нужен доступ в
                # рабочий каталог (tempfile создаёт его с правами 0700).
                # Каталог одноразовый, со случайным именем, живёт секунды и
                # содержит только сгенерированный скрипт и данные самого
                # участника; удаляет его владелец, а не пользователь песочницы.
                os.chmod(workdir, 0o777)
            (workdir / "main.py").write_text(code, encoding="utf-8")
            (workdir / "_runner.py").write_text(
                _RUNNER.format(
                    mem=MEM_LIMIT_BYTES, cpu=cpu_limit, fsize=FSIZE_LIMIT,
                    max_out=MAX_STDOUT_CHARS, max_art=MAX_ARTIFACTS,
                    max_art_bytes=MAX_ARTIFACT_BYTES,
                ),
                encoding="utf-8",
            )
            for name, blob in (files or {}).items():
                safe = os.path.basename(str(name))
                if safe and safe not in ("main.py", "_runner.py"):
                    (workdir / safe).write_bytes(blob)

            proc = await asyncio.create_subprocess_exec(
                *_build_command(workdir),
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,   # своя группа процессов — убиваем целиком
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(workdir),
                    "MPLBACKEND": "Agg",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    # Одна библиотека — один поток: 4 vCPU делятся между
                    # двумя расчётами и боевым API, а не съедаются numpy.
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                },
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=wall_limit)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                await proc.wait()
                return {
                    "ok": False,
                    "error": f"Расчёт не уложился в {wall_limit} с и был остановлен. "
                             "Упростите модель или уменьшите число точек.",
                    "code": code,
                }
    finally:
        # Слот освобождается при любом исходе, включая таймаут и исключение —
        # иначе одна ошибка навсегда съедала бы место в очереди.
        semaphore.release()

    text = (err or b"").decode("utf-8", errors="replace")
    marker = "__AIOS_RESULT__"
    if marker not in text:
        tail = text[-1500:].strip()
        return {
            "ok": False,
            "error": "Расчёт завершился аварийно (вероятно, превышен лимит памяти "
                     "или процессорного времени)." + (f"\n{tail}" if tail else ""),
            "code": code,
        }

    try:
        payload = json.loads(text.rsplit(marker, 1)[1])
    except ValueError:
        return {"ok": False, "error": "Не удалось разобрать результат расчёта", "code": code}

    payload["code"] = code
    return payload
