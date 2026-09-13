"""drop employee face profiles

Revision ID: 9b1d2c3e4f5a
Revises: 35b6cea0040e
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b1d2c3e4f5a"
down_revision: Union[str, Sequence[str], None] = "35b6cea0040e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop unused employee face profile table."""
    op.execute("DROP TABLE IF EXISTS employee_face_profiles")


def downgrade() -> None:
    """Recreate employee face profile table."""
    op.create_table(
        "employee_face_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("face_embedding_id", sa.String(length=255), nullable=False),
        sa.Column("photo_url", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("face_embedding_id"),
    )
    op.create_index(
        op.f("ix_employee_face_profiles_company_id"),
        "employee_face_profiles",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_employee_face_profiles_employee_id"),
        "employee_face_profiles",
        ["employee_id"],
        unique=False,
    )
