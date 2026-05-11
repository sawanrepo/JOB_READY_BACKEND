"""Refactor interview usage columns

Revision ID: d5c6408d4d44
Revises: 6e4cbf1e381b
Create Date: 2026-05-12 02:08:06.834859

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5c6408d4d44'
down_revision: Union[str, Sequence[str], None] = '6e4cbf1e381b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Rename columns to preserve data
    op.alter_column('users', 'mock_interviews_left_this_month', new_column_name='mock_interviews_left')
    op.alter_column('users', 'audio_interviews_left_this_month', new_column_name='audio_interviews_left')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('users', 'mock_interviews_left', new_column_name='mock_interviews_left_this_month')
    op.alter_column('users', 'audio_interviews_left', new_column_name='audio_interviews_left_this_month')


