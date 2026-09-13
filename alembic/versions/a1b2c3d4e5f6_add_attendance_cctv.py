"""add attendance cctv

Revision ID: a1b2c3d4e5f6
Revises: 9b1d2c3e4f5a
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9b1d2c3e4f5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create attendance_cctv table when it does not already exist."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "attendance_cctv" in inspector.get_table_names():
        return

    op.create_table(
        "attendance_cctv",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("employee_id", sa.String(length=36), nullable=False),
        sa.Column("log_type", sa.String(length=10), nullable=False),
        sa.Column("time_in", sa.Time(), nullable=True),
        sa.Column("time_out", sa.Time(), nullable=True),
        sa.Column("latlong_in", sa.String(length=255), nullable=True),
        sa.Column("address_in", sa.Text(), nullable=True),
        sa.Column("latlong_out", sa.String(length=255), nullable=True),
        sa.Column("address_out", sa.Text(), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("checkin_status", sa.String(length=225), nullable=True),
        sa.Column("checkout_status", sa.String(length=255), nullable=True),
        sa.Column("attendance_status", sa.String(length=50), nullable=False),
        sa.Column("photo_in", sa.String(length=255), nullable=True),
        sa.Column("photo_out", sa.String(length=255), nullable=True),
        sa.Column("attendance_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("explanation", sa.String(length=255), nullable=True),
        sa.Column("shift_assignment_id", sa.String(length=36), nullable=True),
        sa.Column("leave_type_id", sa.String(length=36), nullable=True),
        sa.Column("leave_id", sa.String(length=36), nullable=True),
        sa.Column("attendance_request_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="approved", nullable=False),
        sa.Column("createdAt", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updatedAt", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("out_date", sa.Date(), nullable=True),
        sa.Column("explanation_out", sa.String(length=255), nullable=True),
        sa.Column("client_reference_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_attendance_cctv_company_id"), "attendance_cctv", ["company_id"], unique=False)
    op.create_index(op.f("ix_attendance_cctv_employee_id"), "attendance_cctv", ["employee_id"], unique=False)
    op.create_index(
        op.f("ix_attendance_cctv_attendance_date"),
        "attendance_cctv",
        ["attendance_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_attendance_cctv_client_reference_id"),
        "attendance_cctv",
        ["client_reference_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop attendance_cctv table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "attendance_cctv" not in inspector.get_table_names():
        return

    op.drop_index(op.f("ix_attendance_cctv_client_reference_id"), table_name="attendance_cctv")
    op.drop_index(op.f("ix_attendance_cctv_attendance_date"), table_name="attendance_cctv")
    op.drop_index(op.f("ix_attendance_cctv_employee_id"), table_name="attendance_cctv")
    op.drop_index(op.f("ix_attendance_cctv_company_id"), table_name="attendance_cctv")
    op.drop_table("attendance_cctv")
