"""Hold processing and outbound writes after restoring a historical snapshot."""
from alembic import op
import sqlalchemy as sa

revision='115a1d0c2026'
down_revision='113a1d0c2026'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('restore_checkpoints',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('active_key',sa.String(10),nullable=True,unique=True),
        sa.Column('backup_id',sa.String(36),nullable=False),
        sa.Column('snapshot_at',sa.DateTime(),nullable=False),
        sa.Column('restored_at',sa.DateTime(),nullable=False),
        sa.Column('released_at',sa.DateTime(),nullable=True))


def downgrade():
    op.drop_table('restore_checkpoints')
