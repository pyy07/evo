from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from evo_api.db.session import get_db
from evo_api.models.entities import Capability, CapabilityStatus
from evo_api.services.invoke import CapabilityError, invoke_capability

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class InvokeBody(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def list_caps(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = (
        db.query(Capability)
        .filter(Capability.status == CapabilityStatus.active)
        .order_by(Capability.id)
        .all()
    )
    return {
        "capabilities": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "category": r.category,
                "version": r.version,
                "input_schema": r.input_schema,
            }
            for r in rows
        ]
    }


@router.post("/{capability_id}/invoke")
def invoke(
    capability_id: str,
    body: InvokeBody,
    db: Session = Depends(get_db),
    x_actor: str = Header(default="agent", alias="X-Actor"),
) -> dict[str, Any]:
    try:
        result = invoke_capability(db, capability_id, body.input, actor=x_actor)
    except CapabilityError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"capability_id": capability_id, "result": result}