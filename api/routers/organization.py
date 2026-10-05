"""HTTP-слой раздела «Об организации» (админ-панель).

GET    /api/admin/organization                 — профиль, показатели и документы;
PUT    /api/admin/organization/profile         — раздел «Основная информация»;
PUT    /api/admin/organization/metrics         — раздел «Показатели деятельности»;
POST   /api/admin/organization/documents       — загрузка документа (multipart);
PUT    /api/admin/organization/documents/{id}  — правка названия/категории/.md-описания;
DELETE /api/admin/organization/documents/{id}  — удаление документа.

Правки записываются в исходные JSON data/organization/ (см. runtime/organization.py);
дата последнего обновления проставляется автоматически (ДД.ММ.ГГГГ). Доступ —
только роли «Администратор» (require_admin из роутера auth).
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..runtime import organization
from .auth import require_admin

router = APIRouter(prefix="/api")


class SectionUpdate(BaseModel):
    """Тело PUT /api/admin/organization/profile и .../metrics.

    values — словарь {id поля: значение}; неизвестные идентификаторы
    игнорируются, типы проверяет OrganizationStore.
    """

    values: dict = Field(default_factory=dict)
    source: str = Field(default="", max_length=300)


class DocumentUpdate(BaseModel):
    """Тело PUT /api/admin/organization/documents/{id}: правка метаданных."""

    title: str | None = Field(default=None, min_length=1, max_length=organization.TITLE_MAX_CHARS)
    category: str | None = Field(default=None, max_length=30)
    summary: str | None = Field(default=None, max_length=organization.SUMMARY_MAX_CHARS)


def _store() -> organization.OrganizationStore:
    return organization.get_store()


@router.get("/admin/organization")
def get_organization_data(_: dict = Depends(require_admin)):
    """Все данные раздела для интерфейса админ-панели."""
    store = _store()
    return {
        "profile": store.load_profile(),
        "metrics": store.load_metrics(),
        "documents": [organization.public_document(d) for d in store.load_documents()],
        "categories": organization.CATEGORIES,
    }


@router.put("/admin/organization/profile")
def save_profile(req: SectionUpdate, _: dict = Depends(require_admin)):
    """Сохраняет раздел «Основная информацию» в исходный profile.json."""
    try:
        return _store().save_profile(req.values, req.source)
    except organization.OrganizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.put("/admin/organization/metrics")
def save_metrics(req: SectionUpdate, _: dict = Depends(require_admin)):
    """Сохраняет показатели деятельности в исходный metrics.json."""
    try:
        return _store().save_metrics(req.values, req.source)
    except organization.OrganizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/admin/organization/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    category: str = Form(...),
    title: str = Form(default=""),
    summary: str = Form(default=""),
    _: dict = Depends(require_admin),
):
    """Загружает файл в organization/objects/ и регистрирует его в documents.json."""
    data = await file.read()
    try:
        doc = _store().add_document(
            filename=file.filename or "",
            category=category,
            title=title,
            summary=summary,
            data=data,
        )
    except organization.OrganizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return organization.public_document(doc)


@router.put("/admin/organization/documents/{doc_id}")
def update_document(doc_id: str, req: DocumentUpdate, _: dict = Depends(require_admin)):
    """Правка названия, категории или короткого .md-описания документа."""
    try:
        doc = _store().update_document(
            doc_id, title=req.title, category=req.category, summary=req.summary
        )
    except organization.OrganizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    return organization.public_document(doc)


@router.delete("/admin/organization/documents/{doc_id}")
def delete_document(doc_id: str, _: dict = Depends(require_admin)):
    """Удаляет документ: запись реестра и файл из objects/."""
    if not _store().delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Документ не найден")
    return {"ok": True}
