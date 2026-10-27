"""Файловое хранилище пользователей (серверная авторизация AIOS).

Каждый пользователь — отдельный JSON-файл в AIOS_RUNTIME_DIR/users; запись
атомарная (временный файл + replace), по образцу хранилища сессий.

Пароли хранятся только в виде PBKDF2-HMAC-SHA256 со случайной солью на
пользователя (hashlib из стандартной библиотеки — без новых зависимостей).

Роли: «Администратор» и «Пользователь». При первом старте (пустой каталог)
создаётся стартовый администратор из прежней демо-учётной записи.

Регистрация требует подтверждения e-mail: new записи создаются с
confirmed=False; seed-админ и все записи до введения подтверждения —
считаются подтверждёнными (чтение через .get("confirmed", True)).
"""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from .config import get_settings

ROLE_ADMIN = "Администратор"
ROLE_USER = "Пользователь"
ROLES = (ROLE_ADMIN, ROLE_USER)

PBKDF2_ITERATIONS = 120_000
USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
LOGIN_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # упрощённая проверка e-mail

# Стартовый администратор: создаётся один раз при пустом хранилище,
# чтобы админ-панель была доступна сразу после развёртывания.
# Логин и пароль берутся из настроек (AIOS_ADMIN_LOGIN / AIOS_ADMIN_PASSWORD),
# чтобы на сервере не оставался пароль, известный из открытых исходников.
SEED_ADMIN_NAME = "Администратор платформы"


class UserError(ValueError):
    """Некорректные данные пользователя (логин, пароль, имя, роль)."""


class DuplicateLogin(UserError):
    """Логин уже занят."""


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()


class UserStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._seed_admin()

    def _path(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise UserError("Некорректный идентификатор пользователя")
        return self.root / f"{user_id}.json"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def _seed_admin(self) -> None:
        if any(self.root.glob("*.json")):
            return
        settings = get_settings()
        self.create(
            login=settings.admin_login,
            password=settings.admin_password,
            name=SEED_ADMIN_NAME,
            role=ROLE_ADMIN,
            confirmed=True,
        )

    def create(
        self,
        login: str,
        password: str,
        name: str = "",
        role: str = ROLE_USER,
        confirmed: bool = False,
        consent_pdn: bool = False,
        consent_policy: bool = False,
    ) -> dict:
        login = (login or "").strip().lower()
        name = (name or "").strip()
        if not LOGIN_RE.match(login) or len(login) > 120:
            raise UserError("Логин должен быть корректным e-mail адресом (до 120 символов)")
        if len(password or "") < 4:
            raise UserError("Пароль короче 4 символов")
        if name and not 2 <= len(name) <= 80:
            raise UserError("Имя должно быть от 2 до 80 символов")
        if role not in ROLES:
            raise UserError(f"Роль должна быть одной из: {', '.join(ROLES)}")
        if self.get_by_login(login) is not None:
            raise DuplicateLogin("Этот логин уже занят")

        salt = secrets.token_hex(16)
        user = {
            "id": self.new_id(),
            "login": login,
            "name": name,
            "role": role,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "created_at": time.time(),
            "last_login": None,
            "confirmed": confirmed,
            "reminder_overrides": {},
            # Факт согласий по 152-ФЗ, данных при регистрации (ч. 1 ст. 9).
            "consent": {
                "pdn": bool(consent_pdn),
                "policy": bool(consent_policy),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "version": 1,
            },
        }
        self.save(user)
        return user

    def save(self, user: dict) -> None:
        path = self._path(user["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(user, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def public(user: dict) -> dict:
        """Представление для API: без хеша пароля и соли."""
        return {
            "id": user.get("id"),
            "login": user.get("login"),
            "name": user.get("name"),
            "role": user.get("role"),
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
            "confirmed": bool(user.get("confirmed", True)),
        }

    def get(self, user_id: str) -> dict | None:
        try:
            path = self._path(user_id)
        except UserError:
            return None
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _load_all(self) -> list:
        items = []
        for path in self.root.glob("*.json"):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda u: u.get("created_at") or 0)
        return items

    def get_by_login(self, login: str) -> dict | None:
        needle = (login or "").strip().lower()
        for user in self._load_all():
            if user.get("login") == needle:
                return user
        return None

    def list_all(self) -> list:
        """Все регистрации в порядке создания (сначала старые)."""
        return [self.public(u) for u in self._load_all()]

    def count_admins(self) -> int:
        return sum(1 for u in self._load_all() if u.get("role") == ROLE_ADMIN)

    def delete(self, user_id: str) -> bool:
        try:
            path = self._path(user_id)
        except UserError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True

    def set_password(self, user: dict, password: str) -> None:
        """Меняет пароль (новая случайная соль) и сохраняет пользователя."""
        if len(password or "") < 4:
            raise UserError("Пароль короче 4 символов")
        user["salt"] = secrets.token_hex(16)
        user["password_hash"] = _hash_password(password, user["salt"])
        self.save(user)

    def confirm(self, user: dict) -> None:
        """Помечает e-mail подтверждённым (переход по ссылке из письма)."""
        user["confirmed"] = True
        self.save(user)

    def verify_password(self, user: dict, password: str) -> bool:
        """Сравнение в постоянном времени, чтобы не течь по таймингам."""
        expected = user.get("password_hash") or ""
        salt = user.get("salt") or ""
        if not expected or not salt:
            return False
        return hmac.compare_digest(_hash_password(password or "", salt), expected)


def is_admin_user(user_id: str | None) -> bool:
    """True — пользователь с ролью «Администратор» (по id из сессии чата).

    Нужен рантайму (инструменты чата, контекст навыков), где нет
    FastAPI-зависимостей: авторизация приходит как user_id в session.
    """
    if not user_id:
        return False
    user = UserStore(get_settings().runtime_dir / "users").get(user_id)
    return bool(user) and user.get("role") == ROLE_ADMIN
