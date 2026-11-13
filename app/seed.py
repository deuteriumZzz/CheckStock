"""Тестовые данные из ТЗ. Запуск: uv run python -m app.seed

Начальные остатки оформляются как ПРИХОДЫ, поэтому попадают в историю.
Повторный запуск безопасен: уже существующие товары пропускаются целиком."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, atomic
from app.models import Product, Warehouse
from app.services.stock import receive_in_tx

WAREHOUSES = ["Север", "Юг"]
# (артикул, название, Север, Юг)
PRODUCTS = [
    ("SKU-101", "Органайзер", 10, 6),
    ("SKU-102", "Контейнер", 5, 0),
    ("SKU-103", "Корзина", 0, 4),
]


def seed(s: Session) -> None:
    with atomic(s):
        wh = {}
        for name in WAREHOUSES:
            w = s.scalar(select(Warehouse).where(Warehouse.name == name))
            if w is None:
                w = Warehouse(name=name)
                s.add(w)
                s.flush()
            wh[name] = w

    for sku, title, north, south in PRODUCTS:
        if s.scalar(select(Product).where(Product.sku == sku)) is not None:
            continue
        with atomic(s):
            p = Product(sku=sku, name=title)
            s.add(p)
            s.flush()
            for name, qty in (("Север", north), ("Юг", south)):
                if qty:
                    receive_in_tx(s, wh[name].id, p.id, qty)


if __name__ == "__main__":
    with SessionLocal() as session:
        seed(session)
    print("Seed выполнен")
