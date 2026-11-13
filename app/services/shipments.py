"""Перемещения между ФФ и отправки FBO. Обе операции: списали с ФФ -> IN_TRANSIT -> RECEIVED.
Товар в пути не числится ни на одном складе: создание операции и списание — одна транзакция."""

from sqlalchemy.orm import Session

from app.db import atomic
from app.errors import Conflict, NotFound, ValidationError
from app.models import FBO, IN_TRANSIT, RECEIVED, TRANSFER, Operation, Product, Warehouse
from app.services.stock import (
    check_quantity,
    get_active_warehouse,
    lock_inventory,
    lock_operation,
    log_event,
    require_available,
)


def _dispatch(
    s: Session, op_type: str, source: Warehouse, product_id: int, quantity: int, dest_id=None
) -> Operation:
    """Блокируем остаток, проверяем доступное, списываем и создаём операцию в пути."""
    inv = lock_inventory(s, product_id, source.id)
    require_available(s, inv, source, quantity)  # резерв FBS отправить нельзя
    inv.on_hand -= quantity

    op = Operation(
        type=op_type,
        status=IN_TRANSIT,
        product_id=product_id,
        quantity=quantity,
        source_warehouse_id=source.id,
        dest_warehouse_id=dest_id,
    )
    s.add(op)
    s.flush()
    log_event(s, op, f"SHIP_{op_type}", from_warehouse_id=source.id, to_warehouse_id=dest_id)
    s.flush()
    return op


def _lock_in_transit(s: Session, operation_id: int, op_type: str) -> Operation:
    """Блокирует операцию и проверяет переход IN_TRANSIT -> RECEIVED."""
    op = lock_operation(s, operation_id)
    if op.type != op_type:
        raise Conflict(f"Операция {operation_id} не является операцией {op_type}")
    if op.status == RECEIVED:
        raise Conflict(f"Операция {operation_id} уже принята")
    if op.status != IN_TRANSIT:
        raise Conflict(f"Операция {operation_id} не в пути (статус {op.status})")
    return op


def ship_transfer(
    s: Session, source_id: int, dest_id: int, product_id: int, quantity: int
) -> Operation:
    """Отправка перемещения: остаток отправителя уменьшается, у получателя пока ничего.
    Склад-источник может быть архивным (вывоз остатка); получатель — только активный."""
    with atomic(s):
        check_quantity(quantity)
        if source_id == dest_id:
            raise ValidationError("Склады отправления и назначения должны различаться")
        if s.get(Product, product_id) is None:
            raise NotFound(f"Товар {product_id} не найден")
        source = s.get(Warehouse, source_id)
        if source is None:
            raise NotFound(f"Склад {source_id} не найден")
        get_active_warehouse(s, dest_id)
        return _dispatch(s, TRANSFER, source, product_id, quantity, dest_id)


def receive_transfer(s: Session, operation_id: int) -> Operation:
    """IN_TRANSIT -> RECEIVED: остаток получателя растёт, количество «в пути» уменьшается."""
    with atomic(s):
        op = _lock_in_transit(s, operation_id, TRANSFER)
        get_active_warehouse(s, op.dest_warehouse_id)
        inv = lock_inventory(s, op.product_id, op.dest_warehouse_id)
        inv.on_hand += op.quantity
        op.status = RECEIVED
        log_event(
            s,
            op,
            "RECEIVE_TRANSFER",
            from_warehouse_id=op.source_warehouse_id,
            to_warehouse_id=op.dest_warehouse_id,
        )
        s.flush()
        return op


def ship_fbo(s: Session, warehouse_id: int, product_id: int, quantity: int) -> Operation:
    """Отправка на склад маркетплейса: ФФ списывается сразу, товар числится в пути до приёмки."""
    with atomic(s):
        check_quantity(quantity)
        if s.get(Product, product_id) is None:
            raise NotFound(f"Товар {product_id} не найден")
        source = get_active_warehouse(s, warehouse_id)
        return _dispatch(s, FBO, source, product_id, quantity)


def receive_fbo(s: Session, operation_id: int) -> Operation:
    """IN_TRANSIT -> RECEIVED: остатки ФФ не меняются, отправка завершается."""
    with atomic(s):
        op = _lock_in_transit(s, operation_id, FBO)
        op.status = RECEIVED
        log_event(s, op, "RECEIVE_FBO", from_warehouse_id=op.source_warehouse_id)
        s.flush()
        return op
