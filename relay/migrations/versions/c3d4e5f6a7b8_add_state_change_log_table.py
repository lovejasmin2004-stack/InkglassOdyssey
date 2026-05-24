"""add state_change_log table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "state_change_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("change_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("delta", sa.Integer(), nullable=True),
        sa.Column("old_value", sa.Integer(), nullable=True),
        sa.Column("new_value", sa.Integer(), nullable=True),
        sa.Column("npc_id", sa.String(), nullable=True),
        sa.Column("condition_id", sa.String(), nullable=True),
        sa.Column("damage_type", sa.String(), nullable=True),
        sa.Column("rest_type", sa.String(), nullable=True),
        sa.Column("field", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("state_change_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_state_change_log_character_id"), ["character_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_state_change_log_change_type"), ["change_type"], unique=False)
        batch_op.create_index("ix_scl_char_type_created", ["character_id", "change_type", "created_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("state_change_log", schema=None) as batch_op:
        batch_op.drop_index("ix_scl_char_type_created")
        batch_op.drop_index(batch_op.f("ix_state_change_log_change_type"))
        batch_op.drop_index(batch_op.f("ix_state_change_log_character_id"))

    op.drop_table("state_change_log")
