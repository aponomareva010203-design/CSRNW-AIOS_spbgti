"""HTTP-слой страницы «Статус пилотного проекта» (api/routers/pilot.py).

GET   /api/pilot/projects        — строки таблицы пилота (публичный эндпоинт);
PATCH /api/pilot/projects/{id}   — статус артефакта, только «Администратор».

Строки формируются из подтверждённых карточек проектов пользователей
(«Проекты», статус active): тематика — title карточки, ФИО руководителя —
анкета «Кабинета» владельца, иначе имя учётной записи. По каждому артефакту
(rag, models, agents, polygon, social) отдаются статус (0 — не начато,
1 — гипотеза сформулирована, 2 — в процессе создания, 3 — завершено) и текст
гипотезы — пояснение по клику на чип статуса. Статусы и гипотезы берутся из
блока artifacts карточки, а если артефакт «не начат», дополняются из
опубликованной JSON-карточки гипотез (data/hypotheses/hyp-{id}.json, навык
«Гипотезы проекта»: разделы ontology/rag → rag, models → models, agents →
agents, polygons → polygon). Эндпоинт публичный (таблицу видят гости),
поэтому персональные поля карточки (бюджет, участники, задел, файлы)
наружу не отдаются.

Черновики (draft) в таблицу не попадают — карточка появляется на странице
после подтверждения пользователем в навыке «Онбординг проекта».
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..runtime import cabinet, hypotheses, projects
from .auth import get_user_store, require_admin

router = APIRouter(prefix="/api")

# разделы JSON-карточки гипотез → колонка таблицы пилота; колонка rag
# называется «RAG и онтология», поэтому онтология попадает туда же.
# Разделы dashboards/social колонки таблицы не имеют и не отображаются.
HYPOTHESES_SECTIONS = {
    "rag": ("ontology", "rag"),
    "models": ("models",),
    "agents": ("agents",),
    "polygon": ("polygons",),
}


def lead_name(user_id: str | None) -> str:
    """ФИО руководителя проекта: анкета «Кабинета», иначе имя учётной записи."""
    if not user_id:
        return ""
    profile = cabinet.get_store().get_profile(user_id)
    name = cabinet.full_name(profile)
    if name:
        return name
    user = get_user_store().get(user_id)
    return str(user.get("name") or "") if user else ""


def _hypotheses_text(table: dict, sections: tuple[str, ...]) -> str:
    """Текст гипотез разделов карточки гипотез — для пояснения по чипу."""
    parts = []
    for section in sections:
        for entry in table.get(section) or []:
            if not isinstance(entry, dict) or entry.get("skipped"):
                continue
            title = str(entry.get("title") or "").strip()
            statement = str(entry.get("statement") or "").strip()
            if title and statement:
                parts.append(f"{title}: {statement}")
            else:
                parts.append(title or statement)
    text = "\n".join(parts)
    return text[: projects.ARTIFACT_HYPOTHESIS_MAX_CHARS]


def pilot_row(card: dict) -> dict:
    """Строка таблицы пилота из карточки: без персональных полей карточки.

    Артефакты «не начато» дополняются из опубликованной карточки гипотез:
    статус 1 («гипотеза сформулирована») и текст гипотезы. Явно выставленные
    администратором статусы (2 и 3) не трогаем.
    """
    artifacts = projects.clean_artifacts(card.get("artifacts"))
    table = hypotheses.load_hypotheses(card.get("id") or "")
    if table and table.get("published"):
        for key, sections in HYPOTHESES_SECTIONS.items():
            text = _hypotheses_text(table, sections)
            if not text:
                continue
            if artifacts[key]["status"] == projects.ARTIFACT_STATUS_MIN:
                artifacts[key]["status"] = 1
            if not artifacts[key]["hypothesis"]:
                artifacts[key]["hypothesis"] = text
    return {
        "id": card.get("id") or "",
        "title": card.get("title") or "Без названия",
        "lead": lead_name(card.get("user_id")),
        "artifacts": artifacts,
    }


@router.get("/pilot/projects")
def list_pilot_projects():
    """Строки таблицы «Статус пилотного проекта» — активные карточки всех."""
    cards = projects.get_store().list_active()
    return {"items": [pilot_row(c) for c in cards]}


class PilotStatusRequest(BaseModel):
    """Тело PATCH — какой артефакт и какой статус поставить (0–3)."""

    artifact: str
    status: int = Field(ge=0, le=3)


@router.patch("/pilot/projects/{project_id}")
def set_artifact_status(
    project_id: str,
    req: PilotStatusRequest,
    _: dict = Depends(require_admin),
):
    """Статус артефакта проекта — только роль «Администратор».

    Меняет только статус выбранного артефакта; текст гипотезы правит
    владелец карточки (PATCH /api/projects/{id}, навык «Онбординг проекта»).
    """
    if req.artifact not in projects.ARTIFACT_KEYS:
        raise HTTPException(
            status_code=422,
            detail="Артефакт должен быть одним из: " + ", ".join(projects.ARTIFACT_KEYS),
        )
    store = projects.get_store()
    card = store.find(project_id)
    if card is None or card.get("status") != projects.STATUS_ACTIVE:
        raise HTTPException(status_code=404, detail="Проект не найден")
    artifacts = projects.clean_artifacts(card.get("artifacts"))
    artifacts[req.artifact]["status"] = req.status
    saved = store.save(card["user_id"], {"project_id": project_id, "artifacts": artifacts})
    return pilot_row(saved)
