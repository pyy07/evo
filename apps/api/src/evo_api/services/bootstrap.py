from sqlalchemy.orm import Session

from evo_api.db.base import Base
from evo_api.db.session import engine
from evo_api.models.entities import Capability, CapabilityStatus
from evo_api.services.capability_catalog import CAPABILITIES
from evo_api.services.portfolio import ensure_account


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def seed_capabilities(db: Session) -> None:
    for item in CAPABILITIES:
        existing = db.get(Capability, item["id"])
        if existing:
            existing.name = item["name"]
            existing.description = item["description"]
            existing.category = item["category"]
            existing.input_schema = item["input_schema"]
            existing.output_schema = item["output_schema"]
            existing.permission = item["permission"]
            existing.implementation = item["implementation"]
            existing.status = CapabilityStatus.active
        else:
            db.add(
                Capability(
                    id=item["id"],
                    name=item["name"],
                    description=item["description"],
                    category=item["category"],
                    input_schema=item["input_schema"],
                    output_schema=item["output_schema"],
                    permission=item["permission"],
                    implementation=item["implementation"],
                    status=CapabilityStatus.active,
                )
            )
    ensure_account(db)
    db.commit()