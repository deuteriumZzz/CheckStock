"""Чтение остатков. Каждая функция = ровно ОДИН SQL-запрос независимо от числа строк
(JOIN вместо ленивых связей, агрегат на стороне БД) — N+1 здесь невозможен по построению."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import ACTIVE, FBS, IN_TRANSIT, Event, Inventory, Operation, Product, Warehouse


@dataclass(frozen=True)
class StockRow:
    sku: str
    product_name: str
    warehouse_id: int
    warehouse_name: str
    archived: bool
    on_hand: int
    reserved: int
    available: int


def stock_rows(s: Session) -> list[StockRow]:
    rows = s.execute(
        select(
            Product.sku,
            Product.name,
            Warehouse.id,
            Warehouse.name,
            Warehouse.archived_at.is_not(None),
            Inventory.on_hand,
            Inventory.reserved,
            (Inventory.on_hand - Inventory.reserved),
        )
        .join(Product, Product.id == Inventory.product_id)
        .join(Warehouse, Warehouse.id == Inventory.warehouse_id)
        .order_by(Product.sku, Warehouse.name)
    ).all()
    return [StockRow(*r) for r in rows]


def in_transit_totals(s: Session) -> dict[tuple[int, str], int]:
    """(product_id, тип) -> количество в пути. Типы: TRANSFER (между ФФ), FBO, FBS (к клиенту).
    Агрегат по частичному индексу ix_operations_in_transit."""
    rows = s.execute(
        select(Operation.product_id, Operation.type, func.sum(Operation.quantity))
        .where(Operation.status == IN_TRANSIT)
        .group_by(Operation.product_id, Operation.type)
    ).all()
    return {(pid, typ): int(qty) for pid, typ, qty in rows}


@dataclass(frozen=True)
class WarehouseRow:
    id: int
    name: str
    archived: bool
    on_hand: int
    reserved: int


def list_warehouses(s: Session) -> list[WarehouseRow]:
    """Склады с суммарным остатком: LEFT JOIN + GROUP BY, один запрос. Архивные — в конце."""
    rows = s.execute(
        select(
            Warehouse.id,
            Warehouse.name,
            Warehouse.archived_at.is_not(None),
            func.coalesce(func.sum(Inventory.on_hand), 0),
            func.coalesce(func.sum(Inventory.reserved), 0),
        )
        .outerjoin(Inventory, Inventory.warehouse_id == Warehouse.id)
        .group_by(Warehouse.id)
        .order_by(Warehouse.archived_at.is_not(None), Warehouse.name)
    ).all()
    return [WarehouseRow(*r) for r in rows]


def stock_overview(s: Session) -> dict:
    """Экран остатков: товары x склады, физически / резерв / доступно + «в пути».
    Всегда 4 запроса (склады, товары, остатки, в пути) — не зависит от числа товаров и складов.
    Отсутствие строки остатка = 0; архивный склад виден, пока на нём есть товар."""
    warehouses = [w for w in list_warehouses(s) if not w.archived or w.on_hand > 0]
    cells = {
        (pid, wid): (on_hand, reserved)
        for pid, wid, on_hand, reserved in s.execute(
            select(
                Inventory.product_id, Inventory.warehouse_id, Inventory.on_hand, Inventory.reserved
            )
        )
    }
    transit = in_transit_totals(s)
    products = []
    for pid, sku, name in s.execute(
        select(Product.id, Product.sku, Product.name).order_by(Product.sku)
    ):
        row_cells = []
        for w in warehouses:
            on_hand, reserved = cells.get((pid, w.id), (0, 0))
            row_cells.append(
                {
                    "warehouse_id": w.id,
                    "on_hand": on_hand,
                    "reserved": reserved,
                    "available": on_hand - reserved,
                }
            )
        products.append(
            {
                "id": pid,
                "sku": sku,
                "name": name,
                "cells": row_cells,
                "total_on_hand": sum(c["on_hand"] for c in row_cells),
                "in_transit": {t: transit.get((pid, t), 0) for t in ("TRANSFER", "FBO", "FBS")},
            }
        )
    return {
        "warehouses": [{"id": w.id, "name": w.name, "archived": w.archived} for w in warehouses],
        "products": products,
    }


@dataclass(frozen=True)
class OpenOperationRow:
    id: int
    type: str
    status: str
    sku: str
    product_name: str
    quantity: int
    source_warehouse: str | None
    dest_warehouse: str | None
    created_at: datetime


def open_operations(s: Session) -> list[OpenOperationRow]:
    """Незавершённое: активные резервы FBS и всё, что в пути. Один запрос с JOIN-ами."""
    src, dst = aliased(Warehouse), aliased(Warehouse)
    rows = s.execute(
        select(
            Operation.id,
            Operation.type,
            Operation.status,
            Product.sku,
            Product.name,
            Operation.quantity,
            src.name,
            dst.name,
            Operation.created_at,
        )
        .join(Product, Product.id == Operation.product_id)
        .outerjoin(src, src.id == Operation.source_warehouse_id)
        .outerjoin(dst, dst.id == Operation.dest_warehouse_id)
        .where(
            or_(
                Operation.status == IN_TRANSIT,
                and_(Operation.type == FBS, Operation.status == ACTIVE),
            )
        )
        .order_by(Operation.id.desc())
    ).all()
    return [OpenOperationRow(*r) for r in rows]


@dataclass(frozen=True)
class HistoryRow:
    id: int
    type: str
    sku: str
    product_name: str
    quantity: int
    from_warehouse: str | None  # None у FBS/FBO-получателя: клиент / склад маркетплейса
    to_warehouse: str | None
    operation_status: str
    created_at: datetime
    note: str | None  # причина корректировки


def history(
    s: Session, warehouse_id: int | None = None, product_id: int | None = None, limit: int = 100
) -> list[HistoryRow]:
    """История: новые сверху; названия складов и товара подтягиваются JOIN-ами в том же запросе."""
    src, dst = aliased(Warehouse), aliased(Warehouse)
    query = (
        select(
            Event.id,
            Event.type,
            Product.sku,
            Product.name,
            Event.quantity,
            src.name,
            dst.name,
            Operation.status,
            Event.created_at,
            Operation.note,
        )
        .join(Product, Product.id == Event.product_id)
        .join(Operation, Operation.id == Event.operation_id)
        .outerjoin(src, src.id == Event.from_warehouse_id)
        .outerjoin(dst, dst.id == Event.to_warehouse_id)
        .order_by(Event.id.desc())
        .limit(limit)
    )
    if warehouse_id is not None:
        query = query.where(
            or_(Event.from_warehouse_id == warehouse_id, Event.to_warehouse_id == warehouse_id)
        )
    if product_id is not None:
        query = query.where(Event.product_id == product_id)
    return [HistoryRow(*r) for r in s.execute(query).all()]
