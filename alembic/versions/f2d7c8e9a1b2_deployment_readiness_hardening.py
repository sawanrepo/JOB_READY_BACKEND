"""deployment readiness hardening

Revision ID: f2d7c8e9a1b2
Revises: 57d55504e53e
Create Date: 2026-05-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2d7c8e9a1b2'
down_revision: Union[str, Sequence[str], None] = '57d55504e53e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interview_sessions', sa.Column('retakes', sa.JSON(), server_default='{}', nullable=True))
    op.add_column('interview_sessions', sa.Column('processing_status', sa.String(), server_default='idle', nullable=True))
    op.add_column('interview_sessions', sa.Column('result_status', sa.String(), server_default='pending', nullable=True))

    # PostgreSQL partial unique index: one active audio and one active video session per user.
    op.create_index(
        'uq_interview_sessions_user_type_active',
        'interview_sessions',
        ['user_id', 'interview_type'],
        unique=True,
        postgresql_where=sa.text('is_active = true'),
    )


def downgrade() -> None:
    op.drop_index('uq_interview_sessions_user_type_active', table_name='interview_sessions')
    op.drop_column('interview_sessions', 'result_status')
    op.drop_column('interview_sessions', 'processing_status')
    op.drop_column('interview_sessions', 'retakes')
