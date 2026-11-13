"""stream_offers.pinned_published — состояние закрепления на момент публикации

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stream_offers") as batch:
        batch.add_column(sa.Column("pinned_published", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE stream_offers SET pinned_published = pinned")


def downgrade() -> None:
    with op.batch_alter_table("stream_offers") as batch:
        batch.drop_column("pinned_published")
