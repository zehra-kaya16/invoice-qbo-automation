"""Persist encrypted QBO connections and one-use OAuth state."""
from alembic import op
import sqlalchemy as sa

revision = "94a1d0c20260"
down_revision = "87a1d0c20260"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("qbo_connections") as batch:
        batch.add_column(sa.Column("environment", sa.String(20), nullable=False, server_default="sandbox"))
        batch.add_column(sa.Column("client_fingerprint", sa.String(64)))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("token_state", sa.String(20), nullable=False, server_default="ready"))
    op.create_table("qbo_oauth_attempts",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("client_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("qbo_oauth_attempts")
    with op.batch_alter_table("qbo_connections") as batch:
        batch.drop_column("token_state")
        batch.drop_column("revision")
        batch.drop_column("client_fingerprint")
        batch.drop_column("environment")
