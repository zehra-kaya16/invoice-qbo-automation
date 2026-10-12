"""Company keys, persistent browser sessions and OAuth session ownership."""
import uuid
from alembic import op
import sqlalchemy as sa

revision = "100a1d0c2026"
down_revision = "94a1d0c20260"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("api_key", sa.String(100), nullable=True))
        batch.create_unique_constraint("uq_companies_api_key", ["api_key"])
    bind = op.get_bind()
    companies = sa.table("companies", sa.column("id", sa.UUID(as_uuid=False)), sa.column("api_key", sa.String))
    docs = sa.table("documents", sa.column("company_id", sa.UUID(as_uuid=False)), sa.column("workflow_state", sa.JSON))
    mapping = {}
    for row in bind.execute(sa.select(docs.c.company_id, docs.c.workflow_state)):
        state = row.workflow_state or {}
        key = state.get("company_id")
        if key:
            mapping[str(row.company_id).replace("-", "")] = key
    default = str(uuid.uuid5(uuid.NAMESPACE_URL, "invoice-qbo-automation:company:default"))
    for row in bind.execute(sa.select(companies.c.id)):
        flat = str(row.id).replace("-", "")
        key = mapping.get(flat, "default" if flat == default.replace("-", "") else str(row.id))
        bind.execute(companies.update().where(companies.c.id == row.id).values(api_key=key))
    op.create_table("auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False))
    op.create_table("auth_login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_auth_login_attempts_subject_hash", "auth_login_attempts", ["subject_hash"])
    with op.batch_alter_table("qbo_oauth_attempts") as batch:
        batch.add_column(sa.Column("company_id", sa.UUID(as_uuid=False), nullable=True))
        batch.add_column(sa.Column("user_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("session_hash", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_oauth_company", "companies", ["company_id"], ["id"])
    # Old OAuth attempts had no verified owner; never reuse them.
    bind.execute(sa.text("UPDATE qbo_oauth_attempts SET status='cancelled' WHERE status IN ('pending','exchanging')"))


def downgrade():
    with op.batch_alter_table("qbo_oauth_attempts") as batch:
        batch.drop_constraint("fk_oauth_company", type_="foreignkey")
        batch.drop_column("session_hash")
        batch.drop_column("user_id")
        batch.drop_column("company_id")
    op.drop_index("ix_auth_login_attempts_subject_hash", table_name="auth_login_attempts")
    op.drop_table("auth_login_attempts")
    op.drop_table("auth_sessions")
    with op.batch_alter_table("companies") as batch:
        batch.drop_constraint("uq_companies_api_key", type_="unique")
        batch.drop_column("api_key")
