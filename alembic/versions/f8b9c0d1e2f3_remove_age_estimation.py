"""remove unused age estimation configuration"""

from alembic import op
import sqlalchemy as sa


revision = "f8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("cctv_cameras", "age_estimation_enabled")


def downgrade() -> None:
    op.add_column(
        "cctv_cameras",
        sa.Column("age_estimation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("cctv_cameras", "age_estimation_enabled", server_default=None)
