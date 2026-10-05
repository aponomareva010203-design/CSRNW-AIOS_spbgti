"""Конфигурация агентного рантайма AIOS Harness.

Параметры читаются из переменных окружения; при наличии файла .env в корне
проекта он подгружается мини-парсером (без внешних зависимостей).

    AIOS_LLM_BASE_URL        — базовый URL OpenAI-совместимого API (.../v1);
    AIOS_LLM_API_KEY         — ключ доступа;
    AIOS_LLM_MODEL           — модель по умолчанию (переопределяется в /api/chat);
    AIOS_LLM_TIMEOUT         — таймаут чтения ответа модели, сек (120);
    AIOS_MAX_TOOL_ITERATIONS — лимит итераций цикла «модель ↔ инструменты» (8);
    AIOS_APPROVAL_TOOLS     — инструменты, требующие подтверждения пользователем
                               перед исполнением (через запятую). По умолчанию
                               пусто — gate выключен (23.09.2026, решение
                               пользователя); пример широкого набора см. ниже
                               в get_settings;
    AIOS_APPROVAL_TIMEOUT   — сколько секунд ждать решения (55); тишина
                               считается отказом — безопасная сторона;
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

    Автоматизации (n8n, deploy/n8n/SETUP.md):
    AIOS_N8N_BASE_URL        — адрес self-hosted n8n для public API (http://127.0.0.1:5678);
    AIOS_N8N_API_KEY         — ключ public API владельца n8n. Приоритет ниже,
                               чем у настроек из админ-панели (n8n.json).

    Telegram-бот (api/routers/telegram.py):
    AIOS_TG_BOT_TOKEN        — токен бота из @BotFather; пусто — бот выключен;
    AIOS_TG_WEBHOOK_SECRET   — секрет вебхука: Telegram присылает его в заголовке
                               X-Telegram-Bot-Api-Secret-Token (строка из setWebhook),
                               запросы без него отбрасываются;
    AIOS_TG_POLLING          — «1» → long polling (getUpdates) вместо вебхука:
                               локальная разработка без публичного HTTPS. Взаимоисключимо
                               с вебхуком — Telegram отвечает 409, если вебхук установлен;
    AIOS_TG_PROXY            — HTTP/HTTPS-прокси для Bot API (http://host:port), если
                               хостинг режет исходящие соединения к api.telegram.org
                               (типично для российских облаков); socks5:// требует
                               extras httpx[socks]. Пусто — прямые соединения.

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
    user_context_model: str
    request_timeout: float
    max_tool_iterations: int
    approval_tools: tuple[str, ...]
    approval_timeout: float
    tools_root: Path
    runtime_dir: Path
    user_context_run_at: str
    organization_dir: Path
    ontology_data_dir: Path
    rag_data_dir: Path
    agent_data_dir: Path
    icon_data_dir: Path
    admin_login: str
    admin_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_tls: str
    public_base_url: str
    register_ratelimit: int
    email_domain_blocklist: str
    rxn_api_key: str
    rxn_base_url: str
    n8n_base_url: str
    n8n_api_key: str
    semanticscholar_api_key: str
    tg_bot_token: str
    tg_webhook_secret: str
    tg_polling: bool
    tg_proxy: str

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
        # отдельная (обычно более дешёвая) модель для фонового формирования
        # «скрытого контекста о пользователе» (user_context.py); пусто —
        # используется основная модель чата (llm_model/настройки админа)
        user_context_model=_env("AIOS_USER_CONTEXT_MODEL"),
        request_timeout=_env_float("AIOS_LLM_TIMEOUT", 120.0),
        max_tool_iterations=max(1, _env_int("AIOS_MAX_TOOL_ITERATIONS", 8)),
        # подтверждение человеком перед исполнением. По умолчанию ВЫКЛЮЧЕНО
        # (23.09.2026, по решению пользователя: без gate работало лучше);
        # включить — перечислить инструменты в AIOS_APPROVAL_TOOLS, например:
        # "write_file, notify, make_document, save_project_card,
        #  attach_project_file, save_ontology, save_rag"
        approval_tools=tuple(
            t.strip()
            for t in _env("AIOS_APPROVAL_TOOLS", "").split(",")
            if t.strip()
        ),
        approval_timeout=_env_float("AIOS_APPROVAL_TIMEOUT", 55.0),
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
        agent_data_dir=Path(
            _env("AIOS_AGENT_DATA_DIR") or (BASE_DIR / "data")
        ).resolve(),
        # SVG-логотипы артефактов (онтологии/RAG-базы/агенты): файлы
        # {icon_data_dir}/icons/{artifact_id}.svg, публично отдаются
        # статикой монтирования /data как /data/icons/{artifact_id}.svg
        icon_data_dir=Path(
            _env("AIOS_ICON_DATA_DIR") or (BASE_DIR / "data")
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
        # лимит регистраций/повторов письма с одного IP в час; 0 — отключить
        register_ratelimit=_env_int("AIOS_REGISTER_RATELIMIT", 10),
        # домены, регистрация на которые всегда запрещена (через запятую):
        # припаркованные домены с фиктивными ящиками, откуда приходят bounce
        email_domain_blocklist=_env("AIOS_EMAIL_DOMAIN_BLOCKLIST"),
        # IBM RXN for Chemistry: ретросинтез и предсказание продуктов
        # (инструменты rxn_retrosynthesis / rxn_forward). Пусто — инструменты
        # не регистрируются. Ключ: rxn.app.accelerate.science → Settings → API key.
        rxn_api_key=_env("AIOS_RXN_API_KEY"),
        # Инстанс с REST-API, совместимым с облачным IBM RXN (базовый URL
        # вместе с /rxn/api/api/v1). Self-hosted rxn-sandbox не подходит:
        # у него другой интерфейс (Jupyter/Python/MCP), см. .env.example.
        rxn_base_url=_env("AIOS_RXN_BASE_URL").rstrip("/"),
        # n8n для раздела «Автоматизации»: адрес и ключ public API
        # (сильнее них — только настройки из админ-панели, n8n.json)
        n8n_base_url=_env("AIOS_N8N_BASE_URL").rstrip("/"),
        n8n_api_key=_env("AIOS_N8N_API_KEY"),
        # Semantic Scholar Graph API: ключ не обязателен, но поднимает лимит
        # частоты запросов; оформляется бесплатно (semanticscholar.org/product/api).
        semanticscholar_api_key=_env("AIOS_SEMANTICSCHOLAR_API_KEY"),
        # Telegram-бот: токен из @BotFather, секрет вебхука и режим long polling
        # (разработка без публичного HTTPS); см. раздел README «Telegram-бот»
        tg_bot_token=_env("AIOS_TG_BOT_TOKEN"),
        tg_webhook_secret=_env("AIOS_TG_WEBHOOK_SECRET"),
        tg_polling=_env("AIOS_TG_POLLING") == "1",
        tg_proxy=_env("AIOS_TG_PROXY"),
    )
