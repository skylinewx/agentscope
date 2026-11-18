"""Durable per-session user-message queues.

Revision ID: 0005_message_queues
Revises: 0004_sops
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_message_queues"
down_revision = "0004_sops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_queues",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("has_work", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    for column in ("user_id", "created_at", "updated_at", "has_work"):
        op.create_index(
            f"ix_message_queues_{column}", "message_queues", [column]
        )


def downgrade() -> None:
    op.drop_table("message_queues")
