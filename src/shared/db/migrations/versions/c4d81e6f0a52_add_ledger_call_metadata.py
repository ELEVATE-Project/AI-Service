"""add_ledger_call_metadata

Revision ID: c4d81e6f0a52
Revises: a3f7c9d1e284
Create Date: 2026-10-29 00:00:00.000000

Additive only: two nullable columns and one index on ledger_entries. Existing rows,
and rows from callers that send no request metadata, keep NULL in both columns.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'c4d81e6f0a52'
down_revision: Union[str, None] = 'a3f7c9d1e284'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ledger_entries', sa.Column('call_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='Allow-listed ids from the request metadata (session, bot, turn, call kind). Ids only, never text.'))
    op.add_column('ledger_entries', sa.Column('session_id', sa.String(), nullable=True, comment="Caller's chat session id, copied from call_metadata so a session's calls can be found by index."))
    op.create_index(op.f('ix_ledger_entries_session_id'), 'ledger_entries', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ledger_entries_session_id'), table_name='ledger_entries')
    op.drop_column('ledger_entries', 'session_id')
    op.drop_column('ledger_entries', 'call_metadata')
