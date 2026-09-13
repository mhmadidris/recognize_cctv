"""add company timezone setting"""

from alembic import op
import sqlalchemy as sa


revision = "f7a8b9c0d1e2"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cctv_company_settings",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Jakarta"),
    )
    op.alter_column("cctv_company_settings", "timezone", server_default=None)


def downgrade() -> None:
    op.drop_column("cctv_company_settings", "timezone")
