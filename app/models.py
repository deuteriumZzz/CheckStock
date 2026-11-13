from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Типы и статусы операций.
RECEIPT, TRANSFER, FBS, FBO, ADJUSTMENT = "RECEIPT", "TRANSFER", "FBS", "FBO", "ADJUSTMENT"
ACTIVE, CANCELLED, IN_TRANSIT, RECEIVED = "ACTIVE", "CANCELLED", "IN_TRANSIT", "RECEIVED"


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))


class Warehouse(Base):
    """ФФ. «Удаление» = archived_at; строка, остатки и история остаются."""

    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Inventory(Base):
    """on_hand — физически на складе; reserved — часть on_hand под активные FBS-резервы."""

    __tablename__ = "inventory"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    on_hand: Mapped[int] = mapped_column(Integer, server_default="0")
    reserved: Mapped[int] = mapped_column(Integer, server_default="0")
    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id"),
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
        # UNIQUE (product, warehouse) уже индексирует выборку по товару; этот — по складу.
        Index("ix_inventory_warehouse_id", "warehouse_id"),
    )


class Operation(Base):
    """Одна операция с одним товаром. Резерв FBS = операция FBS в статусе ACTIVE.
    «В пути» = сумма операций в статусе IN_TRANSIT (отдельного счётчика нет)."""

    __tablename__ = "operations"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    source_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    dest_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    note: Mapped[str | None] = mapped_column(String(255))  # причина корректировки
    # Ключ повтора от клиента: тот же ключ не создаёт вторую операцию (см. services/idempotency.py).
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "type IN ('RECEIPT','TRANSFER','FBS','FBO','ADJUSTMENT')", name="type_valid"
        ),
        CheckConstraint(
            "status IN ('ACTIVE','CANCELLED','IN_TRANSIT','RECEIVED')", name="status_valid"
        ),
        CheckConstraint(
            "(type = 'RECEIPT' AND source_warehouse_id IS NULL AND dest_warehouse_id IS NOT NULL)"
            " OR (type = 'TRANSFER' AND source_warehouse_id IS NOT NULL"
            " AND dest_warehouse_id IS NOT NULL AND source_warehouse_id <> dest_warehouse_id)"
            " OR (type IN ('FBS','FBO') AND source_warehouse_id IS NOT NULL"
            " AND dest_warehouse_id IS NULL)"
            # корректировка: «+» заполняет склад назначения, «−» склад отправления, ровно один из двух
            " OR (type = 'ADJUSTMENT' AND (source_warehouse_id IS NULL) <> (dest_warehouse_id IS NULL))",
            name="warehouses_match_type",
        ),
        # Частичные индексы: «в пути» и «активные резервы» — малая доля строк,
        # а читаются на каждом экране и при архивации склада.
        Index(
            "ix_operations_in_transit",
            "product_id",
            "type",
            postgresql_where=text("status = 'IN_TRANSIT'"),
        ),
        Index(
            "ix_operations_active_by_source",
            "source_warehouse_id",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class Event(Base):
    """История. Пишется в той же транзакции, что и изменение остатков; не удаляется."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("operations.id"))
    type: Mapped[str] = mapped_column(String(32))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index("ix_events_operation_id", "operation_id"),
        # история склада: WHERE from = X OR to = X (BitmapOr по двум индексам)
        Index("ix_events_from_warehouse_id", "from_warehouse_id"),
        Index("ix_events_to_warehouse_id", "to_warehouse_id"),
    )
