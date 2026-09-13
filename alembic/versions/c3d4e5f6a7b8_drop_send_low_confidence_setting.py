"""drop send low confidence setting

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop send_low_confidence_to_hrms from company settings."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("cctv_company_settings")}
    if "send_low_confidence_to_hrms" in columns:
        op.drop_column("cctv_company_settings", "send_low_confidence_to_hrms")


def downgrade() -> None:
    """Restore send_low_confidence_to_hrms on company settings."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("cctv_company_settings")}
    if "send_low_confidence_to_hrms" not in columns:
        op.add_column(
            "cctv_company_settings",
            sa.Column("send_low_confidence_to_hrms", sa.Boolean(), nullable=True),
        )
