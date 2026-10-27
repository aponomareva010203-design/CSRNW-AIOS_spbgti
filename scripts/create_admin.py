"""Создать администратора платформы или сбросить ему пароль.

Пользователи хранятся не в базе данных, а JSON-файлами в
AIOS_RUNTIME_DIR/users (по умолчанию .aios-runtime/users). Стартовый
администратор создаётся сам только при первом запуске, пока хранилище
пусто; этот скрипт нужен, когда хранилище уже заполнено (например,
первый запуск был без .env) или пароль администратора забыт.

Запуск из корня проекта (с активированным venv):

    python scripts/create_admin.py
    python scripts/create_admin.py my@university.ru МойПароль123

Без аргументов — логин и пароль из .env (AIOS_ADMIN_LOGIN /
AIOS_ADMIN_PASSWORD), а если их нет — admin@university.local / admin12345.
Пользователь с таким логином уже есть — ему ставится новый пароль, роль
«Администратор» и подтверждённый e-mail. Перезапуск сервера не нужен.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.runtime.config import get_settings, load_dotenv  # noqa: E402
from api.runtime.users import ROLE_ADMIN, SEED_ADMIN_NAME, UserError, UserStore  # noqa: E402

DEFAULT_LOGIN = "admin@university.local"
DEFAULT_PASSWORD = "admin12345"


def main() -> int:
    load_dotenv()
    if len(sys.argv) == 3:
        login, password = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 1:
        login = os.environ.get("AIOS_ADMIN_LOGIN") or DEFAULT_LOGIN
        password = os.environ.get("AIOS_ADMIN_PASSWORD") or DEFAULT_PASSWORD
    else:
        print("Использование: python scripts/create_admin.py [логин пароль]")
        return 2

    # пустое хранилище UserStore засевает стартовым администратором из
    # настроек — подставляем наши логин и пароль, чтобы не появился чужой
    os.environ["AIOS_ADMIN_LOGIN"] = login
    os.environ["AIOS_ADMIN_PASSWORD"] = password
    get_settings.cache_clear()
    users_dir = get_settings().runtime_dir / "users"

    try:
        store = UserStore(users_dir)
        user = store.get_by_login(login)
        if user is None:
            store.create(
                login=login,
                password=password,
                name=SEED_ADMIN_NAME,
                role=ROLE_ADMIN,
                confirmed=True,
            )
            action = "создан"
        else:
            store.set_password(user, password)
            user = store.get_by_login(login)
            user["role"] = ROLE_ADMIN
            user["confirmed"] = True
            store.save(user)
            action = "обновлён (новый пароль, роль «Администратор»)"
    except UserError as exc:
        print(f"Ошибка: {exc}")
        return 1

    print(f"Администратор {action}.")
    print(f"  Логин:  {login.strip().lower()}")
    print(f"  Пароль: {password}")
    print(f"  Хранилище: {users_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
