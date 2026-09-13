"""add unique attendance daily record

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_attendance_cctv_company_employee_date",
        "attendance_cctv",
        ["company_id", "employee_id", "attendance_date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_attendance_cctv_company_employee_date",
        "attendance_cctv",
        type_="unique",
    )
