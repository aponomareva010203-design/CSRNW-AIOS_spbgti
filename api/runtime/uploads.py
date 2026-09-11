"""Хранилище вложений рабочего чата (api/runtime/uploads.py).

Каталог AIOS_RUNTIME_DIR/uploads/{user_id}/ — по паре файлов на вложение:
{att-id} (содержимое) и {att-id}.json (метаданные). Файлы намеренно лежат
в .aios-runtime, а не в публичной data/: вложение доступно только владельцу
по Bearer-токену.

Вложение живёт в двух ролях:
    - контекст диалога: текст (pdf/txt/md) подмешивается в сообщение
      пользователя в POST /api/chat (routers/chat.py), агент видит
      содержимое без специальных инструментов;
    - файл карточки проекта: инструмент attach_project_file (runtime/tools.py)
      перемещает вложение в files/ карточки, и оно становится доступным
      для скачивания на странице «Проекты» (routers/projects.py).

Загружаются вложения через POST /api/chat/attachments; лимиты — 20 МБ на
файл (как в RAG) и 5 файлов на сообщение.
"""

import io
import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings
from .projects import USER_ID_RE

ATTACHMENT_ID_RE = re.compile(r"^att-[a-f0-9]{8,12}$")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 МБ на файл — тот же предел, что у RAG
MAX_ATTACHMENTS_PER_MESSAGE = 5
ALLOWED_SUFFIXES = (".pdf", ".txt", ".md", ".docx")
MAX_FILENAME_CHARS = 150
# сколько извлечённого текста подмешивается в сообщение пользователя;
# большего модель за один ход всё равно не прочитает
MAX_EXTRACTED_CHARS = 15_000


class UploadError(ValueError):
    """Некорректные данные вложения."""


def clean_filename(name: str) -> str:
    """Имя файла без пути и опасных символов, обрезанное до лимита."""
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" ._")
    if not name:
        raise UploadError("Пустое имя файла")
    return name[:MAX_FILENAME_CHARS]


def extract_text(filename: str, content: bytes) -> str:
    """Текст вложения для контекста диалога; pdf/txt/md — да, docx — нет."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader  # лениво: остальной рантайм работает и без pypdf

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if lower.endswith((".txt", ".md")):
        return content.decode("utf-8", errors="ignore")
    return ""


class UploadStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ----- низкоуровневые операции -----

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise UploadError("Некорректный идентификатор пользователя")
        return self.root / user_id

    def _meta_path(self, user_id: str, attachment_id: str) -> Path:
        if not ATTACHMENT_ID_RE.match(attachment_id or ""):
            raise UploadError("Некорректный идентификатор вложения")
        return self._dir(user_id) / f"{attachment_id}.json"

    @staticmethod
    def _read_meta(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ----- запись / чтение -----

    def save(self, user_id: str, filename: str, content: bytes) -> dict:
        """Сохраняет вложение владельца, возвращая метаданные-дескриптор."""
        if len(content) > MAX_UPLOAD_BYTES:
            raise UploadError("Файл больше 20 МБ")
        if not content:
            raise UploadError("Пустой файл")
        name = clean_filename(filename)
        if not name.lower().endswith(ALLOWED_SUFFIXES):
            raise UploadError(
                "Поддерживаются файлы .pdf, .txt, .md и .docx"
            )
        attachment_id = "att-" + secrets.token_hex(5)
        directory = self._dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": attachment_id,
            "user_id": user_id,
            "filename": name,
            "size": len(content),
            "created_at": time.time(),
        }
        (directory / attachment_id).write_bytes(content)
        meta_path = self._meta_path(user_id, attachment_id)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def get(self, user_id: str, attachment_id: str) -> dict | None:
        """Метаданные вложения владельца токена; чужие недостижимы."""
        try:
            meta = self._read_meta(self._meta_path(user_id, attachment_id))
        except UploadError:
            return None
        if meta and meta.get("id") != attachment_id:
            return None
        return meta

    def content_path(self, user_id: str, meta: dict) -> Path:
        """Путь к содержимому вложения (метаданные должны существовать)."""
        return self._dir(user_id) / str(meta["id"])

    def resolve_many(self, user_id: str, attachment_ids) -> list[dict]:
        """Метаданные списка вложений сообщения; лишние/чужие/битые — ошибка.

        Клиент присылает ровно то, что показал пользователю чипсами, поэтому
        молча пропустить вложение нельзя: сообщение уйдёт без контекста,
        который пользователь считает приложенным.
        """
        ids = list(attachment_ids or [])
        if len(ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise UploadError(
                f"Не больше {MAX_ATTACHMENTS_PER_MESSAGE} файлов на сообщение"
            )
        metas = []
        seen = set()
        for attachment_id in ids:
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            meta = self.get(user_id, attachment_id)
            if meta is None:
                raise UploadError(f"Вложение не найдено: {attachment_id}")
            metas.append(meta)
        return metas

    def delete(self, user_id: str, attachment_id: str) -> bool:
        """Удаляет вложение владельца; True — если существовало."""
        meta = self.get(user_id, attachment_id)
        if meta is None:
            return False
        self._meta_path(user_id, attachment_id).unlink(missing_ok=True)
        self.content_path(user_id, meta).unlink(missing_ok=True)
        return True


def attachment_context_block(metas: list[dict], contents: list[str]) -> str:
    """Текстовый блок вложений, дописываемый к сообщению пользователя.

    contents — извлечённые тексты в порядке metas (пустая строка, если
    извлечение не поддержано). Служебный блок объясняет модели, что это
    приложенные пользователем файлы, а не часть его речи.
    """
    parts = []
    for meta, text in zip(metas, contents):
        name = meta.get("filename") or "файл"
        # id в заголовке нужен модели: она передаёт его в attach_project_file,
        # когда пользователь просит приложить файл к карточке проекта
        header = f"[Приложенный файл: {name} | id: {meta.get('id')}]"
        if text:
            body = text.strip()[:MAX_EXTRACTED_CHARS]
            marker = "\n\n(текст файла обрезан)" if len(text.strip()) > MAX_EXTRACTED_CHARS else ""
            parts.append(f"{header}\n{body}{marker}")
        else:
            parts.append(f"{header} (текст не извлекается — только хранение и скачивание)")
    return "\n\n".join(parts)


@lru_cache(maxsize=1)
def get_store() -> UploadStore:
    """Синглтон хранилища; в тестах сбрасывается cache_clear()."""
    return UploadStore(get_settings().runtime_dir / "uploads")
