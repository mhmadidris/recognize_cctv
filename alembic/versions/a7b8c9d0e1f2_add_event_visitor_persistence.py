"""Persist Event Visitor CCTV settings and counted events."""
from alembic import op
import sqlalchemy as sa

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    settings = op.create_table(
        "event_visitor_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_source", sa.String(), nullable=True),
        sa.Column("line_position", sa.Float(), nullable=False),
        sa.Column("line_orientation", sa.String(10), nullable=False),
        sa.Column("reverse_direction", sa.Boolean(), nullable=False),
        sa.Column("model_tier", sa.String(1), nullable=False),
    )
    op.bulk_insert(settings, [{"id": 1, "camera_source": None,
                              "line_position": 0.5, "line_orientation": "horizontal",
                              "reverse_direction": False, "model_tier": "m"}])
    op.create_table(
        "event_visitor_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("visitor_id", sa.String(32), nullable=False),
        sa.Column("visitor_label", sa.String(100), nullable=False),
        sa.Column("camera_source", sa.String(), nullable=True),
        sa.Column("gender", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("session_id", "visitor_id", "detected_at"):
        op.create_index(f"ix_event_visitor_events_{column}", "event_visitor_events", [column])


def downgrade():
    op.drop_table("event_visitor_events")
    op.drop_table("event_visitor_settings")
