"""Runtime schema baseline.

Creates exactly the tables the runtime repository uses
(`app.services.runtime_store.metadata`). The previous aspirational schema
(users/projects/knowledge_bases/...) was never used by the application and
conflicted with these table names; it is kept for reference in
migrations/archive/.
"""

from alembic import op

from app.services.runtime_store import metadata

revision = "20260925_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(op.get_bind(), checkfirst=True)
