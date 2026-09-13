"""drop cctv detection logs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop CCTV detection log table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cctv_detection_logs" not in inspector.get_table_names():
        return

    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("cctv_detection_logs")
    }
    for index_name in (
        "ix_cctv_detection_logs_employee_id",
        "ix_cctv_detection_logs_company_id",
        "ix_cctv_detection_logs_branch_id",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="cctv_detection_logs")

    op.drop_table("cctv_detection_logs")


def downgrade() -> None:
    """Recreate CCTV detection log table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cctv_detection_logs" in inspector.get_table_names():
        return

    op.create_table(
        "cctv_detection_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("branch_id", sa.UUID(), nullable=True),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("snapshot_url", sa.String(length=500), nullable=True),
        sa.Column("payload_sent", sa.JSON(), nullable=True),
        sa.Column("payload_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("api_response_status", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["camera_id"], ["cctv_cameras.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cctv_detection_logs_branch_id"), "cctv_detection_logs", ["branch_id"], unique=False)
    op.create_index(op.f("ix_cctv_detection_logs_company_id"), "cctv_detection_logs", ["company_id"], unique=False)
    op.create_index(op.f("ix_cctv_detection_logs_employee_id"), "cctv_detection_logs", ["employee_id"], unique=False)
