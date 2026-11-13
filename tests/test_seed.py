from sqlalchemy import func, select

from app.models import Event, Inventory, Product, Warehouse
from app.seed import seed
from app.services.stock import receive


def stock(s):
    rows = s.execute(
        select(Product.sku, Warehouse.name, Inventory.on_hand, Inventory.reserved)
        .join(Inventory, Inventory.product_id == Product.id)
        .join(Warehouse, Warehouse.id == Inventory.warehouse_id)
    ).all()
    return {(sku, wh): (on_hand, reserved) for sku, wh, on_hand, reserved in rows}


def test_initial_stock_matches_assignment(session):
    seed(session)
    got = stock(session)
    expected = {
        ("SKU-101", "Север"): 10,
        ("SKU-101", "Юг"): 6,
        ("SKU-102", "Север"): 5,
        ("SKU-103", "Юг"): 4,
    }
    assert {k: v[0] for k, v in got.items()} == expected
    # нули из ТЗ (SKU-102/Юг, SKU-103/Север) — отсутствие строки = 0 на складе
    assert all(reserved == 0 for _, reserved in got.values())


def test_seed_is_idempotent_and_keeps_user_stock(session):
    seed(session)
    sku101 = session.scalar(select(Product).where(Product.sku == "SKU-101"))
    north = session.scalar(select(Warehouse).where(Warehouse.name == "Север"))
    receive(session, north.id, sku101.id, 5)  # пользовательский приход: 10 -> 15

    seed(session)  # повторный запуск не должен ни дублировать, ни затирать

    assert stock(session)[("SKU-101", "Север")] == (15, 0)
    assert session.scalar(select(func.count()).select_from(Product)) == 3
    assert session.scalar(select(func.count()).select_from(Warehouse)) == 2


def test_stock_equals_history(session):
    """Остаток на каждом складе равен сумме приходов в истории."""
    seed(session)
    receive(session, 1, 1, 5)
    for (sku, wh), (on_hand, _) in stock(session).items():
        total = session.scalar(
            select(func.coalesce(func.sum(Event.quantity), 0))
            .join(Product, Product.id == Event.product_id)
            .join(Warehouse, Warehouse.id == Event.to_warehouse_id)
            .where(Product.sku == sku, Warehouse.name == wh)
        )
        assert total == on_hand
