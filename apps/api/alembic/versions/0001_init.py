"""init schema via metadata create_all helper

Revision ID: 0001
Revises:
Create Date: 2026-10-08
"""

from typing import Sequence, Union

from alembic import op

from evo_api.db.base import Base
import evo_api.models.entities  # noqa: F401

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)