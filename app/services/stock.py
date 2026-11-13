from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import atomic
from app.errors import Conflict, InsufficientStock, NotFound, ValidationError
from app.models import RECEIPT, RECEIVED, Event, Inventory, Operation, Product, Warehouse


def check_quantity(quantity: int) -> None:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValidationError("Количество должно быть положительным целым числом")


def get_active_warehouse(s: Session, warehouse_id: int) -> Warehouse:
    # FOR SHARE: обычные операции не мешают друг другу, а архивация (FOR UPDATE) ждёт их
    # и наоборот, поэтому склад не успеет уйти в архив между проверкой и записью.
    wh = s.execute(
        select(Warehouse)
        .where(Warehouse.id == warehouse_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if wh is None:
        raise NotFound(f"Склад {warehouse_id} не найден")
    if wh.archived_at is not None:
        raise Conflict(f"Склад «{wh.name}» в архиве: новые операции запрещены")
    return wh


def lock_inventory(s: Session, product_id: int, warehouse_id: int) -> Inventory:
    """Создаёт строку остатка при необходимости и берёт на неё блокировку FOR UPDATE."""
    s.execute(
        insert(Inventory)
        .values(product_id=product_id, warehouse_id=warehouse_id)
        .on_conflict_do_nothing(index_elements=["product_id", "warehouse_id"])
    )
    return s.execute(
        select(Inventory)
        .where(Inventory.product_id == product_id, Inventory.warehouse_id == warehouse_id)
        .with_for_update()
        # После ожидания блокировки нужны свежие значения, а не копия из identity map сессии.
        .execution_options(populate_existing=True)
    ).scalar_one()


def lock_operation(s: Session, operation_id: int) -> Operation:
    """Блокирует операцию. Порядок блокировок везде один: операция -> остатки (по id)."""
    op = s.execute(
        select(Operation)
        .where(Operation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if op is None:
        raise NotFound(f"Операция {operation_id} не найдена")
    return op


def log_event(
    s: Session,
    op: Operation,
    event_type: str,
    from_warehouse_id: int | None = None,
    to_warehouse_id: int | None = None,
) -> None:
    """Запись истории — в той же транзакции, что и изменение остатков."""
    s.add(
        Event(
            operation_id=op.id,
            type=event_type,
            product_id=op.product_id,
            quantity=op.quantity,
            from_warehouse_id=from_warehouse_id,
            to_warehouse_id=to_warehouse_id,
        )
    )


def receive_in_tx(s: Session, warehouse_id: int, product_id: int, quantity: int) -> Operation:
    """Приход без commit — чтобы seed мог объединить несколько приходов в одну транзакцию."""
    check_quantity(quantity)
    if s.get(Product, product_id) is None:
        raise NotFound(f"Товар {product_id} не найден")
    get_active_warehouse(s, warehouse_id)

    inv = lock_inventory(s, product_id, warehouse_id)
    inv.on_hand += quantity
    op = Operation(
        type=RECEIPT,
        status=RECEIVED,
        product_id=product_id,
        quantity=quantity,
        dest_warehouse_id=warehouse_id,
    )
    s.add(op)
    s.flush()
    log_event(s, op, RECEIPT, to_warehouse_id=warehouse_id)
    s.flush()
    return op


def require_available(s: Session, inv: Inventory, wh: Warehouse, quantity: int) -> None:
    """Отказ без изменений, если на выбранном складе не хватает; другой склад сами не выбираем.
    Остатки по другим ФФ показываем одним запросом — как подсказку менеджеру."""
    available = inv.on_hand - inv.reserved
    if quantity <= available:
        return
    others = s.execute(
        select(Warehouse.name, (Inventory.on_hand - Inventory.reserved).label("available"))
        .join(Inventory, Inventory.warehouse_id == Warehouse.id)
        .where(
            Inventory.product_id == inv.product_id,
            Inventory.warehouse_id != wh.id,
            Warehouse.archived_at.is_(None),
            Inventory.on_hand - Inventory.reserved > 0,
        )
        .order_by(Warehouse.name)
    ).all()
    msg = f"Недостаточно товара на складе «{wh.name}»: доступно {available}, запрошено {quantity}."
    if others:
        total = sum(a for _, a in others)
        detail = ", ".join(f"{name} {a}" for name, a in others)
        msg += f" На других ФФ доступно {total} ({detail}); оформите перемещение."
    raise InsufficientStock(msg)


def receive(s: Session, warehouse_id: int, product_id: int, quantity: int) -> Operation:
    with atomic(s):
        return receive_in_tx(s, warehouse_id, product_id, quantity)
