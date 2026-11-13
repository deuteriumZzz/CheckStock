from sqlalchemy import func, select

from app.models import Event, Inventory, Operation


def balance(s, product_id, warehouse_id):
    """(on_hand, reserved) склада; свежее чтение из БД. Нет строки остатка = (0, 0)."""
    row = s.execute(
        select(Inventory.on_hand, Inventory.reserved).where(
            Inventory.product_id == product_id, Inventory.warehouse_id == warehouse_id
        )
    ).one_or_none()
    return tuple(row) if row else (0, 0)


def counts(s):
    """(число операций, число событий) — чтобы проверять, что отказ ничего не записал."""
    return (
        s.scalar(select(func.count()).select_from(Operation)),
        s.scalar(select(func.count()).select_from(Event)),
    )
