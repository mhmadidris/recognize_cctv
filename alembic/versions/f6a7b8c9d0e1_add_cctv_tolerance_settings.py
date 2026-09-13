"""add CCTV attendance tolerance settings

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cctv_company_settings",
        sa.Column("allow_attendance_after_tolerance", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "cctv_company_settings",
        sa.Column("time_tolerance_minutes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("cctv_company_settings", "time_tolerance_minutes")
    op.drop_column("cctv_company_settings", "allow_attendance_after_tolerance")
