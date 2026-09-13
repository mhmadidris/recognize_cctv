"""persist CCTV monitoring mode per company

Revision ID: 1a2b3c4d5e6f
Revises: 0f1e2d3c4b5a
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "0f1e2d3c4b5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("cctv_company_settings")}
    if "monitor_mode" not in columns:
        op.add_column(
            "cctv_company_settings",
            sa.Column("monitor_mode", sa.String(length=20), nullable=False, server_default="attendance"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("cctv_company_settings")}
    if "monitor_mode" in columns:
        op.drop_column("cctv_company_settings", "monitor_mode")
