"""add status and stage indexes

Revision ID: e1f2a3b4c5d6
Revises: c8f2a31d9e74
Create Date: 2026-05-16 14:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c8f2a31d9e74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_sessions_status"), "sessions", ["status"])
    op.create_index(op.f("ix_scenes_status"), "scenes", ["status"])
    op.create_index(op.f("ix_pending_turns_stage"), "pending_turns", ["stage"])


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_turns_stage"), table_name="pending_turns")
    op.drop_index(op.f("ix_scenes_status"), table_name="scenes")
    op.drop_index(op.f("ix_sessions_status"), table_name="sessions")
