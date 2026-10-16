import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from evo_api.db.base import Base
from evo_api.db.session import engine
from evo_api.models.entities import Capability, CapabilityStatus
from evo_api.services.capability_catalog import CAPABILITIES
from evo_api.services.portfolio import ensure_account
from evo_api.services.experiences import backfill_experience_fingerprints
from evo_api.services.seed_experiences import seed_experiences

log = logging.getLogger(__name__)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    try:
        migrate_schema()
    except SQLAlchemyError as exc:
        # 远端 PG WAL/锁拥堵时不应拖死整个 API 启动
        log.warning("migrate_schema skipped due to DB error: %s", exc)


def _pg_set_timeouts(conn) -> None:
    conn.execute(text("SET lock_timeout = '3s'"))
    conn.execute(text("SET statement_timeout = '8s'"))


def migrate_schema() -> None:
    """Add new enum values / columns and remap legacy CR statuses."""
    dialect = engine.dialect.name
    if dialect == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            _pg_set_timeouts(conn)
            for value in ("NoIssue",):
                try:
                    conn.execute(text(f"ALTER TYPE issuetype ADD VALUE IF NOT EXISTS '{value}'"))
                except SQLAlchemyError as exc:
                    log.warning("skip enum %s: %s", value, exc)
            for value in ("pending_dev", "completed", "verified"):
                try:
                    conn.execute(
                        text(
                            f"ALTER TYPE changerequeststatus ADD VALUE IF NOT EXISTS '{value}'"
                        )
                    )
                except SQLAlchemyError as exc:
                    log.warning("skip enum %s: %s", value, exc)
            # 先查 information_schema，列已存在则跳过 ALTER，避免远端 PG WAL/锁拥堵时启动卡死
            wanted = (
                ("change_requests", "verification_notes", "TEXT"),
                ("audit_logs", "agent_run_id", "INTEGER"),
                ("investment_decisions", "usage_notes", "JSONB"),
                ("experiences", "fingerprint", "VARCHAR(200)"),
                ("experiences", "updated_at", "TIMESTAMPTZ"),
            )
            for table, column, col_type in wanted:
                try:
                    exists = conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                        ),
                        {"t": table, "c": column},
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                except SQLAlchemyError as exc:
                    log.warning("skip add column %s.%s: %s", table, column, exc)
    with engine.begin() as conn:
        if dialect == "postgresql":
            _pg_set_timeouts(conn)
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
            exp_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(experiences)")).fetchall()
            }
            if "fingerprint" not in exp_cols:
                conn.execute(text("ALTER TABLE experiences ADD COLUMN fingerprint VARCHAR(200)"))
            if "updated_at" not in exp_cols:
                conn.execute(text("ALTER TABLE experiences ADD COLUMN updated_at DATETIME"))
        for sql in (
            "UPDATE change_requests SET status = 'pending_dev' WHERE status = 'approved'",
            "UPDATE change_requests SET status = 'completed' WHERE status = 'implemented'",
        ):
            try:
                conn.execute(text(sql))
            except SQLAlchemyError as exc:
                log.warning("skip status remap (%s): %s", sql, exc)
        # day_settlements → settlements
        if dialect == "postgresql":
            old = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'day_settlements'"
                )
            ).fetchone()
            new = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'settlements'"
                )
            ).fetchone()
            if old and not new:
                try:
                    conn.execute(text("ALTER TABLE day_settlements RENAME TO settlements"))
                    try:
                        conn.execute(
                            text(
                                "ALTER TABLE settlements RENAME CONSTRAINT "
                                "uq_day_settlements_trade_date TO uq_settlements_trade_date"
                            )
                        )
                    except Exception:  # noqa: BLE001
                        pass
                except SQLAlchemyError as exc:
                    log.warning("skip day_settlements rename: %s", exc)
        else:
            old = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='day_settlements'"
                )
            ).fetchone()
            new = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settlements'"
                )
            ).fetchone()
            if old and not new:
                conn.execute(text("ALTER TABLE day_settlements RENAME TO settlements"))


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
    # 废弃旧能力 id
    legacy = db.get(Capability, "list_day_settlements")
    if legacy:
        legacy.status = CapabilityStatus.disabled
        legacy.implementation = "list_settlements"
    ensure_account(db)
    seed_experiences(db)
    backfill_experience_fingerprints(db)
    db.commit()
