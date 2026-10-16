"""经验沉淀：投资经验追加为主；使用/系统经验按 fingerprint 原地更新。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from evo_api.models.entities import Experience, ExperienceKind
from evo_api.services.market_session import require_review_window

_FP_TAG = re.compile(r"^\s*(\[[^\]]{1,120}\])")


def extract_fingerprint(content: str, explicit: str | None = None) -> str | None:
    raw = (explicit or "").strip()
    if raw:
        return raw[:200]
    m = _FP_TAG.match(content or "")
    if m:
        return m.group(1)[:200]
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_lessons(
    db: Session,
    lessons: Any,
    *,
    review_id: int | None,
    agent_run_id: int | None,
) -> list[int]:
    """写入 lessons。

    - agent/system + fingerprint：同指纹原地更新 content/updated_at
    - investment + fingerprint：已存在则跳过追加（去重）
    - 无 fingerprint：始终追加
    """
    if not lessons:
        return []
    require_review_window()
    ids: list[int] = []
    for item in lessons:
        if isinstance(item, str):
            kind = ExperienceKind.investment
            content = item.strip()
            fingerprint = extract_fingerprint(content)
        elif isinstance(item, dict):
            kind_raw = item.get("kind") or "investment"
            kind = ExperienceKind(kind_raw)
            content = str(item.get("content") or "").strip()
            fingerprint = extract_fingerprint(content, item.get("fingerprint"))
        else:
            continue
        if not content:
            continue

        if fingerprint and kind in (ExperienceKind.agent, ExperienceKind.system):
            existing = (
                db.query(Experience)
                .filter(
                    Experience.kind == kind,
                    Experience.fingerprint == fingerprint,
                )
                .order_by(Experience.id.desc())
                .first()
            )
            if existing:
                existing.content = content
                existing.source_review_id = review_id
                existing.agent_run_id = agent_run_id
                existing.updated_at = _now()
                db.flush()
                ids.append(existing.id)
                continue

        if fingerprint and kind == ExperienceKind.investment:
            existing = (
                db.query(Experience)
                .filter(
                    Experience.kind == kind,
                    Experience.fingerprint == fingerprint,
                )
                .order_by(Experience.id.desc())
                .first()
            )
            if existing:
                ids.append(existing.id)
                continue

        exp = Experience(
            kind=kind,
            fingerprint=fingerprint,
            content=content,
            source_review_id=review_id,
            agent_run_id=agent_run_id,
            updated_at=_now(),
        )
        db.add(exp)
        db.flush()
        ids.append(exp.id)
    return ids


def list_experiences(
    db: Session,
    *,
    kind: str | None = None,
    include_usage: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """默认只返回投资经验；include_usage=true 时追加 agent/system（投资在前）。"""
    limit = max(1, min(int(limit or 20), 100))
    if kind:
        rows = (
            db.query(Experience)
            .filter(Experience.kind == ExperienceKind(kind))
            .order_by(Experience.updated_at.desc(), Experience.id.desc())
            .limit(limit)
            .all()
        )
    elif include_usage:
        # 投资优先填满，再补使用/系统
        inv_cap = max(1, (limit * 3 + 3) // 4)
        inv = (
            db.query(Experience)
            .filter(Experience.kind == ExperienceKind.investment)
            .order_by(Experience.updated_at.desc(), Experience.id.desc())
            .limit(inv_cap)
            .all()
        )
        rest = limit - len(inv)
        usage: list[Experience] = []
        if rest > 0:
            usage = (
                db.query(Experience)
                .filter(Experience.kind.in_([ExperienceKind.agent, ExperienceKind.system]))
                .order_by(Experience.updated_at.desc(), Experience.id.desc())
                .limit(rest)
                .all()
            )
        rows = list(inv) + list(usage)
    else:
        rows = (
            db.query(Experience)
            .filter(Experience.kind == ExperienceKind.investment)
            .order_by(Experience.updated_at.desc(), Experience.id.desc())
            .limit(limit)
            .all()
        )

    return {
        "items": [
            {
                "id": r.id,
                "kind": r.kind.value,
                "fingerprint": r.fingerprint,
                "content": r.content,
                "source_review_id": r.source_review_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "include_usage": bool(include_usage) if not kind else None,
        "note": (
            "默认仅投资经验（kind=investment）。"
            "工具/能力口径请传 include_usage=true 或 kind=agent|system；"
            "使用经验随系统演进按 fingerprint 原地更新，勿当作永久投资结论。"
        ),
    }


def backfill_experience_fingerprints(db: Session) -> int:
    """从正文前缀 [标签] 回填 fingerprint；返回更新条数。"""
    updated = 0
    rows = db.query(Experience).filter(Experience.fingerprint.is_(None)).all()
    for row in rows:
        fp = extract_fingerprint(row.content or "")
        if not fp:
            continue
        row.fingerprint = fp
        if row.updated_at is None:
            row.updated_at = row.created_at or _now()
        updated += 1
    if updated:
        db.flush()
    return updated
