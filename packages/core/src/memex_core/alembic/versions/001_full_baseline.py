"""Full baseline: extensions + all tables via SQLModel.metadata.create_all.

Revision ID: 001_full_baseline
Revises:
Create Date: 2026-02-28
"""

from typing import Sequence, Union

from alembic import op
from sqlmodel import SQLModel

# revision identifiers, used by Alembic.
revision: str = '001_full_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    # Import all models so SQLModel.metadata is fully populated.
    import memex_core.memory.sql_models  # noqa: F401

    SQLModel.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    import memex_core.memory.sql_models  # noqa: F401

    SQLModel.metadata.drop_all(bind=op.get_bind())
    op.execute('DROP EXTENSION IF EXISTS pg_trgm')
    op.execute('DROP EXTENSION IF EXISTS vector')
