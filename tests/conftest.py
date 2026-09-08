"""Общая настройка тестов: корень репозитория в sys.path, тестовый runtime-каталог."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AIOS_RUNTIME_DIR", str(ROOT / ".aios-runtime-test"))
