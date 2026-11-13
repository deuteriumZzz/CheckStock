"""idempotency: ключ повтора в операциях

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_operations_idempotency_key", "operations", ["idempotency_key"])


def downgrade() -> None:
    op.drop_constraint("uq_operations_idempotency_key", "operations", type_="unique")
    op.drop_column("operations", "idempotency_key")
