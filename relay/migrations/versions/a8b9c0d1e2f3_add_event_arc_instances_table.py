"""add event_arc_instances table

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-05-22 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_arc_instances",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("blueprint_id", sa.String(), nullable=False),
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False, server_default="authored"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("current_phase_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("phases", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "blueprint_id", "id", name="uq_eai_char_bp_id"),
    )
    op.create_index("ix_eai_character_status", "event_arc_instances", ["character_id", "status"])
    op.create_index("ix_eai_world_id", "event_arc_instances", ["world_id"])


def downgrade() -> None:
    op.drop_index("ix_eai_world_id", table_name="event_arc_instances")
    op.drop_index("ix_eai_character_status", table_name="event_arc_instances")
    op.drop_table("event_arc_instances")
