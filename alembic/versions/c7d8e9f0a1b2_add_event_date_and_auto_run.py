"""Add event date and automatic run mode.

Revision ID: c7d8e9f0a1b2
Revises: f8b9c0d1e2f3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "f8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("event_date", sa.Date(), nullable=True))
    op.add_column(
        "events",
        sa.Column("auto_run", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("events", "auto_run")
    op.drop_column("events", "event_date")
