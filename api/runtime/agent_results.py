"""Архив результатов запусков агентов (api/runtime/agent_results.py).

Запуск агента по расписанию или кнопкой «Запустить сейчас» даёт отчёт. Раньше
от него оставались первые 500 символов в записи и результаты инструментов,
сведённые к true: полный отчёт видел только тот, кто сидел на странице. Здесь
он сохраняется целиком, как данные. Файл (Word, HTML) собирается из них в
момент скачивания, поэтому улучшенное оформление применится и к старым
результатам.

Хранение, как у остальных объектов платформы, — JSON-файлы:

  .aios-runtime/results/{owner_id}/{agent_id}/res-ДАТА-ВРЕМЯ-хвост.json       отчёт целиком
  .aios-runtime/results/{owner_id}/{agent_id}/res-ДАТА-ВРЕМЯ-хвост.meta.json  карточка для списка

Каталог лежит вне .aios-runtime/agents/{owner}/{agent}/: удаление агента стирает
его папку целиком, а результаты должны его пережить.

Доступ — только владелец агента (проверяет роутер agents.py).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from threading import Lock

KEEP_LAST = 100            # запусков на агента; старые уходят первыми
BODY_LIMIT = 400_000       # символов отчёта: с графиками SVG он бывает большим
SUMMARY_LIMIT = 300
ATTACH_BEFORE = 5          # сек: вложение могли создать чуть раньше отметки старта
ATTACH_AFTER = 60          # и записать чуть позже конца прогона
MAX_ATTACHMENTS = 20

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ID_RE = re.compile(r"^res-\d{8}-\d{6}-[0-9a-f]{4}$")

TRIGGER_LABELS = {"schedule": "По расписанию", "manual": "Вручную"}


def run_title(trigger: str, started_at: float) -> str:
    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(started_at))
    return f"{TRIGGER_LABELS.get(trigger, 'Запуск')} · {when}"


def _summary(body: str) -> str:
    text = re.sub(r"```.*?```", " ", body, flags=re.S)
    text = re.sub(r"[*#>|`_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:SUMMARY_LIMIT]


class ResultStore:
    def __init__(self, root: Path, uploads_root: Path | None = None) -> None:
        self._root = Path(root)
        self._uploads = Path(uploads_root) if uploads_root else None
        self._lock = Lock()

    def _dir(self, owner_id: str, agent_id: str, create: bool = False) -> Path:
        for key in (owner_id, agent_id):
            if not _KEY_RE.match(str(key or "")):
                raise ValueError(f"недопустимый идентификатор: {key!r}")
        directory = self._root / owner_id / agent_id
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _attachments(self, owner_id: str, started_at: float, finished_at: float) -> list[dict]:
        """Файлы владельца, созданные за время прогона (make_document и др.).

        Ищем по хранилищу вложений, а не по тексту ответа: модель может не
        упомянуть файл, а он создан и должен быть доступен для скачивания."""
        found: list[dict] = []
        if self._uploads is None:
            return found
        directory = self._uploads / owner_id
        if not directory.is_dir():
            return found
        low, high = started_at - ATTACH_BEFORE, finished_at + ATTACH_AFTER
        for meta_path in sorted(directory.glob("att-*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = float(meta.get("created_at") or 0)
            except (OSError, ValueError, TypeError):
                continue
            if low <= created <= high and meta.get("id"):
                found.append({"id": meta["id"], "filename": meta.get("filename", ""),
                              "size": meta.get("size", 0)})
        return found[:MAX_ATTACHMENTS]

    def add(self, owner_id: str, agent_id: str, *, trigger: str, status: str,
            started_at: float, finished_at: float, agent_version=None,
            params: dict | None = None, title: str = "", body: str = "",
            error: str = "", method: list | None = None) -> dict:
        body = str(body or "")
        truncated = len(body) > BODY_LIMIT
        if truncated:
            body = body[:BODY_LIMIT]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
        result_id = f"res-{stamp}-{uuid.uuid4().hex[:4]}"
        full = {
            "id": result_id, "agent_id": agent_id, "agent_version": agent_version,
            "trigger": str(trigger or ""), "status": str(status or ""),
            "started_at": started_at, "finished_at": finished_at,
            "title": str(title or "")[:200], "summary": _summary(body),
            "params": params or {}, "body": body, "body_truncated": truncated,
            "error": str(error or "")[:2000],
            "attachments": self._attachments(owner_id, started_at, finished_at),
            "method": list(method or [])[:60],
        }
        meta = {k: full[k] for k in ("id", "agent_id", "agent_version", "trigger", "status",
                                     "started_at", "finished_at", "title", "summary", "error")}
        meta["attachments_count"] = len(full["attachments"])
        meta["body_chars"] = len(body)
        with self._lock:
            directory = self._dir(owner_id, agent_id, create=True)
            self._write(directory / f"{result_id}.json", full)
            self._write(directory / f"{result_id}.meta.json", meta)
            self._trim(directory)
        return meta

    @staticmethod
    def _metas(directory: Path) -> list:
        """Карточки результатов, новые сверху. Порядок по started_at, а не по
        имени файла: в имени время с точностью до секунды."""
        found = []
        for path in directory.glob("res-*.meta.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                found.append((float(meta.get("started_at") or 0), path.name, path, meta))
            except (OSError, ValueError, TypeError):
                continue
        found.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return found

    def _trim(self, directory: Path) -> None:
        for _started, _name, path, _meta in self._metas(directory)[KEEP_LAST:]:
            stem = path.name[: -len(".meta.json")]
            for suffix in (".meta.json", ".json"):
                try:
                    (directory / (stem + suffix)).unlink()
                except OSError:
                    pass

    def list(self, owner_id: str, agent_id: str, limit: int = 50) -> list[dict]:
        try:
            directory = self._dir(owner_id, agent_id)
        except ValueError:
            return []
        if not directory.is_dir():
            return []
        return [meta for _s, _n, _p, meta in self._metas(directory)[: max(1, limit)]]

    def get(self, owner_id: str, agent_id: str, result_id: str) -> dict | None:
        if not _ID_RE.match(str(result_id or "")):
            return None
        try:
            path = self._dir(owner_id, agent_id) / f"{result_id}.json"
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


_STORE: ResultStore | None = None


def get_result_store() -> ResultStore:
    global _STORE
    if _STORE is None:
        from .config import get_settings

        runtime = get_settings().runtime_dir
        _STORE = ResultStore(runtime / "results", runtime / "uploads")
    return _STORE
