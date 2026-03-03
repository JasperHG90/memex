"""Remove opinions, rename experience→event.

Revision ID: 002_remove_opinions_rename_event
Revises: 001_full_baseline
Create Date: 2026-03-02

Removes:
- evidence_log table
- confidence_score_fact_type_check constraint
- opinion from fact_type CHECK

Updates:
- fact_type 'opinion' → 'world'
- fact_type 'experience' → 'event'
- fact_type CHECK constraint
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '002_remove_opinions_rename_event'
down_revision: Union[str, None] = '001_full_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop CHECK constraints first (they block the data updates)
    # Drop confidence CHECK (may not exist on all installations)
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE memory_units DROP CONSTRAINT IF EXISTS confidence_score_fact_type_check;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END $$;
    """)

    # Drop old fact_type CHECK
    op.execute("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN (
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'memory_units'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%fact_type%'
            ) LOOP
                EXECUTE 'ALTER TABLE memory_units DROP CONSTRAINT ' || r.conname;
            END LOOP;
        END $$;
    """)

    # 2. Reclassify opinions as world facts
    op.execute("UPDATE memory_units SET fact_type = 'world' WHERE fact_type = 'opinion'")

    # 3. Rename experience → event
    op.execute("UPDATE memory_units SET fact_type = 'event' WHERE fact_type = 'experience'")

    # 4. Drop evidence_log table
    op.execute('DROP TABLE IF EXISTS evidence_log')

    # 5. Add new fact_type CHECK
    op.execute("""
        ALTER TABLE memory_units
        ADD CONSTRAINT memory_units_fact_type_check
        CHECK (fact_type IN ('world', 'event', 'observation'))
    """)


def downgrade() -> None:
    # Reverse: event → experience, restore old CHECK
    op.execute("UPDATE memory_units SET fact_type = 'experience' WHERE fact_type = 'event'")

    op.execute("""
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN (
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'memory_units'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%fact_type%'
            ) LOOP
                EXECUTE 'ALTER TABLE memory_units DROP CONSTRAINT ' || r.conname;
            END LOOP;
        END $$;
    """)
    op.execute("""
        ALTER TABLE memory_units
        ADD CONSTRAINT memory_units_fact_type_check
        CHECK (fact_type IN ('world', 'experience', 'opinion', 'observation'))
    """)
