"""adjustments: тип операции ADJUSTMENT и поле note

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OLD_MATCH = (
    "(type = 'RECEIPT' AND source_warehouse_id IS NULL AND dest_warehouse_id IS NOT NULL)"
    " OR (type = 'TRANSFER' AND source_warehouse_id IS NOT NULL"
    " AND dest_warehouse_id IS NOT NULL AND source_warehouse_id <> dest_warehouse_id)"
    " OR (type IN ('FBS','FBO') AND source_warehouse_id IS NOT NULL"
    " AND dest_warehouse_id IS NULL)"
)
NEW_MATCH = (
    OLD_MATCH
    + " OR (type = 'ADJUSTMENT' AND (source_warehouse_id IS NULL) <> (dest_warehouse_id IS NULL))"
)


def upgrade() -> None:
    op.add_column("operations", sa.Column("note", sa.String(255), nullable=True))
    op.drop_constraint("type_valid", "operations", type_="check")
    op.create_check_constraint(
        "type_valid", "operations", "type IN ('RECEIPT','TRANSFER','FBS','FBO','ADJUSTMENT')"
    )
    op.drop_constraint("warehouses_match_type", "operations", type_="check")
    op.create_check_constraint("warehouses_match_type", "operations", NEW_MATCH)


def downgrade() -> None:
    # Откат невозможен, пока в таблице есть корректировки: их надо удалить вручную.
    op.drop_constraint("warehouses_match_type", "operations", type_="check")
    op.create_check_constraint("warehouses_match_type", "operations", OLD_MATCH)
    op.drop_constraint("type_valid", "operations", type_="check")
    op.create_check_constraint(
        "type_valid", "operations", "type IN ('RECEIPT','TRANSFER','FBS','FBO')"
    )
    op.drop_column("operations", "note")
