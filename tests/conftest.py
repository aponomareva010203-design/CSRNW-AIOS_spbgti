"""Общая настройка тестов: корень репозитория в sys.path, тестовый runtime-каталог."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AIOS_RUNTIME_DIR", str(ROOT / ".aios-runtime-test"))
# тесты входят стартовым администратором с этими учётными данными
os.environ.setdefault("AIOS_ADMIN_LOGIN", "m.kharitonov@csr-nw.ru")
os.environ.setdefault("AIOS_ADMIN_PASSWORD", "1234")
# тесты много раз регистрируют пользователей с одного IP — лимит выключаем
os.environ["AIOS_REGISTER_RATELIMIT"] = "0"
