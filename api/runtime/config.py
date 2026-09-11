"""Конфигурация агентного рантайма AIOS Harness.

Параметры читаются из переменных окружения; при наличии файла .env в корне
проекта он подгружается мини-парсером (без внешних зависимостей).

    AIOS_LLM_BASE_URL        — базовый URL OpenAI-совместимого API (.../v1);
    AIOS_LLM_API_KEY         — ключ доступа;
    AIOS_LLM_MODEL           — модель по умолчанию (переопределяется в /api/chat);
    AIOS_LLM_TIMEOUT         — таймаут чтения ответа модели, сек (120);
    AIOS_MAX_TOOL_ITERATIONS — лимит итераций цикла «модель ↔ инструменты» (8);
    AIOS_TOOLS_ROOT          — корень песочницы файловых инструментов (./data);
    AIOS_RUNTIME_DIR         — каталог рантайма: сессии и служебные файлы (./.aios-runtime);
    AIOS_USER_CONTEXT_RUN_AT  — время ежедневного обновления скрытого «Контекста
                                о пользователе» по московскому времени, «ЧЧ:ММ» («23:00»);
                                контекст для каждого пользователя пересобирается
                                не чаще раза в сутки и только при новых сессиях/упоминаниях;
    AIOS_ORGANIZATION_DIR    — каталог раздела «Об организации» (./data/organization):
                               profile.json, metrics.json, documents.json и objects/ с файлами
                               документов (в тестах перенаправляется во временный каталог);
    AIOS_ONTOLOGY_DATA_DIR   — каталог публичных данных онтологий (./data): сюда навык
                               «Конструктор онтологий» публикует карточки (ontologies.json)
                               и графы (ontologies/objects/{id}.json); в тестах — временный;
    AIOS_RAG_DATA_DIR        — каталог публичных данных RAG-баз (./data): сюда навык
                               «Конструктор RAG» публикует карточки (rag_bases.json);
                               в тестах — временный;
    AIOS_ADMIN_LOGIN         — логин стартового администратора (m.kharitonov@csr-nw.ru);
    AIOS_ADMIN_PASSWORD      — его пароль. Задаётся на сервере в .env; по умолчанию «1234»
                               — только для локальной разработки, в открытых исходниках.

    Подтверждение e-mail при регистрации (SMTP):
    AIOS_SMTP_HOST           — хост SMTP-сервера; пусто → dev-фолбэк (ссылка в лог);
    AIOS_SMTP_PORT           — порт (465);
    AIOS_SMTP_USER/PASSWORD  — учётные данные для входа на SMTP (пароль приложения);
    AIOS_SMTP_FROM           — адрес отправителя писем (по умолчанию = AIOS_SMTP_USER);
    AIOS_SMTP_TLS            — «ssl» (неявный TLS, порт 465) | «starttls» (587) | «none»;
    AIOS_PUBLIC_BASE_URL     — публичный адрес сайта для ссылок в письмах.

Без базового URL/ключа рантайм стартует с mock-провайдером:
пайплайн (сессии, инструменты, SSE) полностью работает офлайн.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def load_dotenv(path: Path | None = None) -> dict:
    """Мини-парсер .env: строки KEY=VALUE, # — комментарии.

    Найденные ключи попадают в os.environ через setdefault,
    то есть реальные переменные окружения имеют приоритет.
    """
    env_file = path or BASE_DIR / ".env"
    loaded: dict = {}
    if not env_file.is_file():
        return loaded
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


@dataclass(frozen=True)
class Settings:
    """Иммутабельный снимок настроек рантайма."""

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    request_timeout: float
    max_tool_iterations: int
    tools_root: Path
    runtime_dir: Path
    user_context_run_at: str
    organization_dir: Path
    ontology_data_dir: Path
    rag_data_dir: Path
    admin_login: str
    admin_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_tls: str
    public_base_url: str

    @property
    def has_real_provider(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return (value if value is not None else default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единственный снимок настроек на процесс; в тестах сбрасывается cache_clear()."""
    load_dotenv()
    return Settings(
        llm_base_url=_env("AIOS_LLM_BASE_URL").rstrip("/"),
        llm_api_key=_env("AIOS_LLM_API_KEY"),
        llm_model=_env("AIOS_LLM_MODEL"),
        request_timeout=_env_float("AIOS_LLM_TIMEOUT", 120.0),
        max_tool_iterations=max(1, _env_int("AIOS_MAX_TOOL_ITERATIONS", 8)),
        tools_root=Path(_env("AIOS_TOOLS_ROOT") or (BASE_DIR / "data")).resolve(),
        runtime_dir=Path(_env("AIOS_RUNTIME_DIR") or (BASE_DIR / ".aios-runtime")).resolve(),
        user_context_run_at=_env("AIOS_USER_CONTEXT_RUN_AT") or "23:00",
        organization_dir=Path(
            _env("AIOS_ORGANIZATION_DIR") or (BASE_DIR / "data" / "organization")
        ).resolve(),
        ontology_data_dir=Path(
            _env("AIOS_ONTOLOGY_DATA_DIR") or (BASE_DIR / "data")
        ).resolve(),
        rag_data_dir=Path(
            _env("AIOS_RAG_DATA_DIR") or (BASE_DIR / "data")
        ).resolve(),
        admin_login=_env("AIOS_ADMIN_LOGIN") or "m.kharitonov@csr-nw.ru",
        admin_password=_env("AIOS_ADMIN_PASSWORD") or "1234",
        smtp_host=_env("AIOS_SMTP_HOST"),
        smtp_port=_env_int("AIOS_SMTP_PORT", 465),
        smtp_user=_env("AIOS_SMTP_USER"),
        smtp_password=_env("AIOS_SMTP_PASSWORD"),
        smtp_from=_env("AIOS_SMTP_FROM") or _env("AIOS_SMTP_USER"),
        smtp_tls=_env("AIOS_SMTP_TLS") or "ssl",
        public_base_url=_env("AIOS_PUBLIC_BASE_URL") or "",
    )
