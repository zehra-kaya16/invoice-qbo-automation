"""Persist API document IDs, source bytes and workflow snapshots.

Revision ID: 87a1d0c20260
Revises: 381b08b83576
"""

from alembic import op
import sqlalchemy as sa

revision = "87a1d0c20260"
down_revision = "381b08b83576"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("api_id", sa.String(100), nullable=True))
        batch.add_column(sa.Column("workflow_state", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("source_content", sa.LargeBinary(), nullable=True))
        batch.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_unique_constraint("uq_documents_api_id", ["api_id"])


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("uq_documents_api_id", type_="unique")
        batch.drop_column("revision")
        batch.drop_column("source_content")
        batch.drop_column("workflow_state")
        batch.drop_column("api_id")
