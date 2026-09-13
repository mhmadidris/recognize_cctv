"""add CCTV camera AI optimization settings

Revision ID: 0f1e2d3c4b5a
Revises: 8b85581ccb78
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0f1e2d3c4b5a"
down_revision: Union[str, Sequence[str], None] = "8b85581ccb78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("cctv_cameras")}

    if "roi_enabled" not in columns:
        op.add_column(
            "cctv_cameras",
            sa.Column("roi_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "roi_polygon" not in columns:
        op.add_column("cctv_cameras", sa.Column("roi_polygon", sa.JSON(), nullable=True))
    if "clahe_enabled" not in columns:
        op.add_column(
            "cctv_cameras",
            sa.Column("clahe_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "age_estimation_enabled" not in columns:
        op.add_column(
            "cctv_cameras",
            sa.Column("age_estimation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "detection_confidence_threshold" not in columns:
        op.add_column("cctv_cameras", sa.Column("detection_confidence_threshold", sa.Float(), nullable=True))
    if "recognition_similarity_threshold" not in columns:
        op.add_column("cctv_cameras", sa.Column("recognition_similarity_threshold", sa.Float(), nullable=True))
    if "reid_similarity_threshold" not in columns:
        op.add_column("cctv_cameras", sa.Column("reid_similarity_threshold", sa.Float(), nullable=True))
    if "track_confirm_hits" not in columns:
        op.add_column("cctv_cameras", sa.Column("track_confirm_hits", sa.Integer(), nullable=True))
    if "min_face_width" not in columns:
        op.add_column("cctv_cameras", sa.Column("min_face_width", sa.Integer(), nullable=True))
    if "min_face_height" not in columns:
        op.add_column("cctv_cameras", sa.Column("min_face_height", sa.Integer(), nullable=True))
    if "min_face_brightness" not in columns:
        op.add_column("cctv_cameras", sa.Column("min_face_brightness", sa.Float(), nullable=True))
    if "min_face_blur" not in columns:
        op.add_column("cctv_cameras", sa.Column("min_face_blur", sa.Float(), nullable=True))
    if "adaptive_frame_skip_enabled" not in columns:
        op.add_column(
            "cctv_cameras",
            sa.Column("adaptive_frame_skip_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "target_inference_ms" not in columns:
        op.add_column("cctv_cameras", sa.Column("target_inference_ms", sa.Integer(), nullable=True))
    if "max_frame_skip" not in columns:
        op.add_column("cctv_cameras", sa.Column("max_frame_skip", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("cctv_cameras")}

    for column_name in (
        "age_estimation_enabled",
        "max_frame_skip",
        "target_inference_ms",
        "adaptive_frame_skip_enabled",
        "min_face_blur",
        "min_face_brightness",
        "min_face_height",
        "min_face_width",
        "track_confirm_hits",
        "reid_similarity_threshold",
        "recognition_similarity_threshold",
        "detection_confidence_threshold",
        "clahe_enabled",
        "roi_polygon",
        "roi_enabled",
    ):
        if column_name in columns:
            op.drop_column("cctv_cameras", column_name)
