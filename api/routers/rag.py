"""HTTP-слой RAG-баз (api/routers/rag.py).

GET    /api/rag                        — RAG-базы текущего пользователя (свои карточки);
GET    /api/rag/{id}                   — одна RAG-база владельца токена;
PATCH  /api/rag/{id}                   — правка карточки (частичный словарь полей);
POST   /api/rag/{id}/publish           — публикация в каталог «Данные и знания»
                                          (владелец, требует ≥1 готового документа);
POST   /api/rag/{id}/documents         — загрузка документа (multipart), ставит
                                          статус queued и запускает индексацию фоном;
GET    /api/rag/{id}/documents         — статусы документов базы (для страницы
                                          загрузки — опрос без повторных полных карточек);
DELETE /api/rag/{id}                   — удаление, только роль «Администратор»
                                          (любой RAG-базы по id, вместе с публикацией).

RAG-базы конструируются навыком «Конструктор RAG» через инструменты get_rag /
save_rag в рабочем чате (конфигурация — тема, ограничения, типовые вопросы);
сами документы загружаются здесь, на отдельной странице (data/skills/
rag-builder.md направляет туда пользователя), не через чат. Индексация —
текст → чанки → эмбеддинги → Qdrant (runtime/indexer.py) — идёт в фоне
(BackgroundTasks), пользователь опрашивает статус через GET .../documents.
Собственные RAG-базы адресуются по идентификатору владельца из Bearer-токена
(get_current_user) — из запроса не принимается, чужие недостижимы.
"""

import re
import secrets

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, HTTPException, UploadFile

from ..runtime import indexer, rag, team
from ..runtime.rag import RagError
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/api")

DOC_ID_RE = re.compile(r"^doc-[a-f0-9]{8,12}$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 МБ на файл — разумный предел для регламентов/статей


def _new_doc_id() -> str:
    return "doc-" + secrets.token_hex(5)


def _extract_text(filename: str, content: bytes) -> str:
    """Извлекает текст из загруженного файла; поддерживает .txt/.md и .pdf."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader
        import io

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return content.decode("utf-8", errors="ignore")


@router.get("/rag")
def list_rag(user: dict = Depends(get_current_user)):
    """RAG-базы владельца токена, свежие сверху."""
    store = rag.get_store()
    cards = store.list(user["id"])
    return {"items": [store.summary(c) for c in cards]}


@router.get("/rag/{rag_id}")
def get_rag_card(rag_id: str, user: dict = Depends(get_current_user)):
    """Одна RAG-база владельца токена (полная карточка)."""
    card = rag.get_store().get(user["id"], rag_id)
    if card is None:
        raise HTTPException(status_code=404, detail="RAG-база не найдена")
    return rag.get_store().public_card(card)


@router.patch("/rag/{rag_id}")
def update_rag(
    rag_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка карточки владельцем токена.

    Тело — частичный словарь полей (name, domain, description, author, tags,
    project_id, ontology_id, corpus_types, corpus_volume, corpus_format,
    access_restrictions, sensitive, typical_queries, citation_required,
    owner, update_frequency, status): переданные поля перезаписываются,
    остальные сохраняются.

    Чужая база доступна участнику команды владельца с правом правки
    RAG-систем (удалять её может только администратор).
    """
    store = rag.get_store()
    owner_id = user["id"]
    if store.get(owner_id, rag_id) is None:
        found = store.find(rag_id)
        if found is None or not team.get_store().can_edit(
            found.get("user_id") or "", user["id"], "rag"
        ):
            raise HTTPException(status_code=404, detail="RAG-база не найдена")
        owner_id = found["user_id"]
    try:
        saved = store.save(owner_id, dict(patch or {}, rag_id=rag_id))
    except RagError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return store.public_card(saved)


@router.post("/rag/{rag_id}/publish")
def publish_rag(rag_id: str, user: dict = Depends(get_current_user)):
    """Публикация RAG-базы в каталог «Данные и знания» (владелец токена).

    Проверяет полноту (валидация require_complete: нужен минимум один
    документ в статусе ready), переводит в active и выкладывает карточку
    в общий каталог; идемпотентно. Открытый доступ на чтение (модель А):
    после публикации базу может подключить в чате любой пользователь.
    """
    store = rag.get_store()
    if store.get(user["id"], rag_id) is None:
        raise HTTPException(status_code=404, detail="RAG-база не найдена")
    try:
        saved = store.save(user["id"], {"rag_id": rag_id, "status": "active"})
    except RagError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "ok": True,
        "rag": store.public_card(saved),
        "catalog_entry": store.catalog_entry(saved),
    }


@router.post("/rag/{rag_id}/documents", status_code=201)
async def upload_document(
    rag_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Загружает документ в черновик/базу владельца токена, ставит в очередь.

    Отвечает сразу (статус queued), извлечение текста и индексация идут
    в фоне (BackgroundTasks) — не блокируют HTTP-ответ. Прогресс опрашивать
    через GET /api/rag/{id}/documents.
    """
    store = rag.get_store()
    card = store.get(user["id"], rag_id)
    if card is None:
        raise HTTPException(status_code=404, detail="RAG-база не найдена")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Файл больше 20 МБ")
    filename = file.filename or "документ"

    doc_id = _new_doc_id()
    documents = list(card.get("documents") or [])
    documents.append({"doc_id": doc_id, "filename": filename, "status": "queued"})
    try:
        store.save(user["id"], {**card, "documents": documents})
    except RagError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    async def _process() -> None:
        try:
            text = _extract_text(filename, content)
        except Exception as exc:  # извлечение текста — внешний код (pypdf), ловим широко
            store.update_document(
                user["id"], rag_id, doc_id,
                {"status": rag.DOC_STATUS_ERROR, "error": f"Не удалось извлечь текст: {exc}"[:500]},
            )
            return
        await indexer.index_document(user["id"], rag_id, doc_id, filename, text)

    background_tasks.add_task(_process)
    return {"ok": True, "doc_id": doc_id, "status": "queued"}


@router.get("/rag/{rag_id}/documents")
def list_documents(rag_id: str, user: dict = Depends(get_current_user)):
    """Статусы документов базы — для опроса страницей загрузки."""
    card = rag.get_store().get(user["id"], rag_id)
    if card is None:
        raise HTTPException(status_code=404, detail="RAG-база не найдена")
    return {"items": card.get("documents") or []}


@router.delete("/rag/{rag_id}")
def delete_rag(rag_id: str, _: dict = Depends(require_admin)):
    """Удаление RAG-базы — только роль «Администратор», любой по id.

    Не удаляет коллекцию в Qdrant (векторное хранилище вне зоны
    ответственности RagStore) — это делается отдельно при необходимости.
    """
    store = rag.get_store()
    card = store.find(rag_id)
    if card is None:
        raise HTTPException(status_code=404, detail="RAG-база не найдена")
    store.delete(card["user_id"], rag_id)
    return {"ok": True, "deleted": rag_id}
