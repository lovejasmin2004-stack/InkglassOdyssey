"""add current_region_id and region_id columns

Revision ID: f150ff510bac
Revises: 62c142535a35
Create Date: 2026-05-19 07:35:25.248937

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f150ff510bac"
down_revision: str | Sequence[str] | None = "62c142535a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add current_region_id to characters, region_id to transaction_log."""
    with op.batch_alter_table("characters") as batch_op:
        batch_op.add_column(sa.Column("current_region_id", sa.String(), nullable=True))

    with op.batch_alter_table("transaction_log") as batch_op:
        batch_op.add_column(sa.Column("region_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove current_region_id from characters, region_id from transaction_log."""
    with op.batch_alter_table("transaction_log") as batch_op:
        batch_op.drop_column("region_id")

    with op.batch_alter_table("characters") as batch_op:
        batch_op.drop_column("current_region_id")
