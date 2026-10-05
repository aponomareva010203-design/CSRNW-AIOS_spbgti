"""Хранилище Telegram-интеграции: привязки учётных записей, коды привязки,
состояния личных чатов. Файлы в AIOS_RUNTIME_DIR/telegram (вне git и вне
публично раздающегося /data), запись атомарная — как у сессий и пользователей.

  links.json  — tg_user_id → учётная запись платформы. Бот работает от её
                имени: личные настройки LLM, учёт расхода токенов, кабинет.
  codes.json  — одноразовые коды привязки из /start (TTL 15 минут); код
                подтверждается на платформе («Настройки» → Telegram или
                deep-link #tg-link=КОД), затем уничтожается.
  chats.json  — состояние личного чата: закреплённая AIOS-сессия, выбранный
                агент (skill_ids) и RAG-база (rag_ids) — как в веб-чате.

Пароль через бота не вводится никогда: привязка — единственный способ
связать Telegram с учётной записью, ключи и пароли платформы в Telegram
не уходят.
"""

import json
import re
import secrets
import time
from pathlib import Path

CODE_TTL = 900.0  # 15 минут на ввод кода
CODE_RE = re.compile(r"^[A-F0-9]{8}$")


class TelegramStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- общая механика -----------------------------------------------------

    def _read(self, name: str) -> dict:
        """Файл или свежий пустой каркас: вызывающий вправе мутировать
        items (setdefault) — общий словарь-дефолт здесь недопустим."""
        path = self.root / name
        if not path.is_file():
            return {"items": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": {}}
        return data if isinstance(data, dict) and isinstance(data.get("items"), dict) else {"items": {}}

    def _write(self, name: str, data: dict) -> None:
        path = self.root / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # --- привязки -----------------------------------------------------------

    def link_of(self, tg_user_id: int) -> dict | None:
        """Привязка Telegram-аккаунта: {user_id, username, linked_at} или None."""
        items = self._read("links.json").get("items") or {}
        link = items.get(str(tg_user_id))
        return link if isinstance(link, dict) and link.get("user_id") else None

    def user_of(self, tg_user_id: int) -> str | None:
        link = self.link_of(tg_user_id)
        return link.get("user_id") if link else None

    def account_of(self, user_id: str) -> dict | None:
        """Обратная связь: привязка по учётной записи платформы."""
        items = self._read("links.json").get("items") or {}
        for link in items.values():
            if link.get("user_id") == user_id:
                return link
        return None

    def bind(self, tg_user_id: int, user_id: str, username: str = "",
             first_name: str = "") -> None:
        """Привязывает Telegram к учётной записи (повторная привязка — замена)."""
        data = self._read("links.json")
        items = data.setdefault("items", {})
        # Telegram привязывается к одной учётке: старые связи этого tg-аккаунта
        # перезаписываются, у одной учётки не может быть двух Telegram
        for key, link in list(items.items()):
            if link.get("user_id") == user_id and key != str(tg_user_id):
                items.pop(key)
        items[str(tg_user_id)] = {
            "user_id": user_id,
            "username": username or "",
            "first_name": first_name or "",
            "linked_at": time.time(),
        }
        self._write("links.json", data)

    def unbind_tg(self, tg_user_id: int) -> str | None:
        """Отвязывает Telegram; возвращает id учётки или None."""
        data = self._read("links.json")
        items = data.get("items") or {}
        link = items.pop(str(tg_user_id), None)
        if not link:
            return None
        self._write("links.json", data)
        return link.get("user_id")

    def unbind_user(self, user_id: str) -> int | None:
        """Отвязка со стороны платформы; возвращает tg_user_id или None."""
        data = self._read("links.json")
        items = data.get("items") or {}
        found = None
        for key, link in items.items():
            if link.get("user_id") == user_id:
                found = int(key) if key.isdigit() else None
                items.pop(key)
                break
        if found is None:
            return None
        self._write("links.json", data)
        return found

    # --- коды привязки ------------------------------------------------------

    def issue_code(self, tg_user_id: int, username: str = "",
                   first_name: str = "") -> str:
        """Одноразовый код привязки для /start; истёкшие коды подчищаются."""
        data = self._read("codes.json")
        items = data.setdefault("items", {})
        now = time.time()
        for code, rec in list(items.items()):
            if rec.get("expires_at", 0) < now:
                items.pop(code)
        code = secrets.token_hex(4).upper()  # 8 символов, читается без путаницы
        items[code] = {
            "tg_user_id": tg_user_id,
            "username": username or "",
            "first_name": first_name or "",
            "expires_at": now + CODE_TTL,
        }
        self._write("codes.json", data)
        return code

    def consume_code(self, code: str) -> dict | None:
        """Снимает код с учёта (одноразовый); None — нет кода/истёк."""
        code = (code or "").strip().upper()
        if not CODE_RE.match(code):
            return None
        data = self._read("codes.json")
        items = data.get("items") or {}
        rec = items.pop(code, None)
        if rec is None:
            return None
        self._write("codes.json", data)
        if rec.get("expires_at", 0) < time.time():
            return None
        return rec

    # --- состояния личных чатов ----------------------------------------------

    def chat_state(self, chat_id: int | str) -> dict:
        items = self._read("chats.json").get("items") or {}
        state = items.get(str(chat_id))
        if not isinstance(state, dict):
            state = {}
        return {
            "session_id": state.get("session_id") or "",
            "skill_ids": list(state.get("skill_ids") or []),
            "rag_ids": list(state.get("rag_ids") or []),
            # варианты последнего ```aios-choices-блока: callback ch:{i}
            # отправляет выбранный текст как сообщение пользователя
            "choices": list(state.get("choices") or []),
        }

    def set_chat_state(self, chat_id: int | str, *, session_id: str | None = None,
                       skill_ids: list | None = None, rag_ids: list | None = None,
                       choices: list | None = None) -> None:
        data = self._read("chats.json")
        items = data.setdefault("items", {})
        key = str(chat_id)
        state = items.get(key) if isinstance(items.get(key), dict) else {}
        if session_id is not None:
            state["session_id"] = session_id
        if skill_ids is not None:
            state["skill_ids"] = list(skill_ids)
        if rag_ids is not None:
            state["rag_ids"] = list(rag_ids)
        if choices is not None:
            state["choices"] = list(choices)
        state["updated_at"] = time.time()
        items[key] = state
        self._write("chats.json", data)

    def clear_chat_session(self, chat_id: int | str) -> None:
        """«Новый диалог»: сессия отвязывается, агент и база остаются."""
        self.set_chat_state(chat_id, session_id="")
