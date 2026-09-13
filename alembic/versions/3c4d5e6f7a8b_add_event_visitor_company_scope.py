"""scope Event Visitor data by company

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, Sequence[str], None] = "2b3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("event_visitor_settings", "event_visitor_sessions", "event_visitor_events"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "company_id" not in columns:
            op.add_column(table, sa.Column("company_id", sa.String(length=36), nullable=True))
            op.create_index(f"ix_{table}_company_id", table, ["company_id"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("event_visitor_events", "event_visitor_sessions", "event_visitor_settings"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "company_id" in columns:
            op.drop_index(f"ix_{table}_company_id", table_name=table)
            op.drop_column(table, "company_id")
