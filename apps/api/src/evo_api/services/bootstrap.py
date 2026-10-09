from sqlalchemy import text
from sqlalchemy.orm import Session

from evo_api.db.base import Base
from evo_api.db.session import engine
from evo_api.models.entities import Capability, CapabilityStatus
from evo_api.services.capability_catalog import CAPABILITIES
from evo_api.services.portfolio import ensure_account
from evo_api.services.seed_experiences import seed_experiences


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    migrate_schema()


def migrate_schema() -> None:
    """Add new enum values / columns and remap legacy CR statuses."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for value in ("NoIssue",):
                conn.execute(text(f"ALTER TYPE issuetype ADD VALUE IF NOT EXISTS '{value}'"))
            for value in ("pending_dev", "completed", "verified"):
                conn.execute(
                    text(f"ALTER TYPE changerequeststatus ADD VALUE IF NOT EXISTS '{value}'")
                )
            for col_sql in (
                "ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS verification_notes TEXT",
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS agent_run_id INTEGER",
                "ALTER TABLE investment_decisions ADD COLUMN IF NOT EXISTS usage_notes JSONB",
            ):
                conn.execute(text(col_sql))
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'change_requests'"
                if dialect == "postgresql"
                else "SELECT 1 FROM sqlite_master WHERE type='table' AND name='change_requests'"
            )
        ).fetchone()
        if not tables:
            return
        if dialect == "sqlite":
            cr_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(change_requests)")).fetchall()
            }
            if "verification_notes" not in cr_cols:
                conn.execute(text("ALTER TABLE change_requests ADD COLUMN verification_notes TEXT"))
            audit_cols = {
                row[1] for row in conn.execute(text("PRAGMA table_info(audit_logs)")).fetchall()
            }
            if "agent_run_id" not in audit_cols:
                conn.execute(text("ALTER TABLE audit_logs ADD COLUMN agent_run_id INTEGER"))
            dec_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(investment_decisions)")).fetchall()
            }
            if "usage_notes" not in dec_cols:
                conn.execute(text("ALTER TABLE investment_decisions ADD COLUMN usage_notes JSON"))
        conn.execute(
            text("UPDATE change_requests SET status = 'pending_dev' WHERE status = 'approved'")
        )
        conn.execute(
            text("UPDATE change_requests SET status = 'completed' WHERE status = 'implemented'")
        )


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
    seed_experiences(db)
    db.commit()
