"""add question_started_at to interview_sessions

Revision ID: a1f9c8d2e3b4
Revises: 6c561be3166b
Create Date: 2026-05-20 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f9c8d2e3b4'
down_revision: Union[str, Sequence[str], None] = '6c561be3166b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_sessions', sa.Column('question_started_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('interview_sessions', 'question_started_at')
