"""HTTP-уровень: те же сценарии через настоящий API, плюс формат ошибок и число запросов."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.api.routes import get_session
from app.main import app
from app.services.warehouses import create_warehouse
from tests.invariants import assert_consistent


@pytest.fixture
def client(engine, ids):
    factory = sessionmaker(engine, expire_on_commit=False)

    def override():
        with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def cell(client, sku, warehouse_name):
    data = client.get("/api/stock").json()
    col = next(i for i, w in enumerate(data["warehouses"]) if w["name"] == warehouse_name)
    product = next(p for p in data["products"] if p["sku"] == sku)
    c = product["cells"][col]
    return c["on_hand"], c["reserved"], c["available"]


def transit(client, sku):
    data = client.get("/api/stock").json()
    return next(p for p in data["products"] if p["sku"] == sku)["in_transit"]


def test_pages_and_docs_are_served(client):
    assert "CheckStock" in client.get("/").text
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_initial_stock_shows_all_six_values_with_explicit_zeros(client):
    data = client.get("/api/stock").json()
    assert [w["name"] for w in data["warehouses"]] == ["Север", "Юг"]
    got = {
        p["sku"]: [(c["on_hand"], c["reserved"], c["available"]) for c in p["cells"]]
        for p in data["products"]
    }
    assert got == {
        "SKU-101": [(10, 0, 10), (6, 0, 6)],
        "SKU-102": [(5, 0, 5), (0, 0, 0)],  # нулевая ячейка из ТЗ показана явно
        "SKU-103": [(0, 0, 0), (4, 0, 4)],
    }


def test_full_scenario_over_http(client, session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]

    receipt = client.post("/api/receipts", json={"warehouse_id": n, "product_id": p, "quantity": 5})
    assert receipt.status_code == 201
    assert cell(client, "SKU-101", "Север") == (15, 0, 15)

    res = client.post(
        "/api/fbs/reservations", json={"warehouse_id": n, "product_id": p, "quantity": 3}
    )
    assert res.status_code == 201
    assert cell(client, "SKU-101", "Север") == (15, 3, 12)
    assert_consistent(session)

    assert client.post(f"/api/fbs/{res.json()['id']}/ship").json()["status"] == "IN_TRANSIT"
    assert cell(client, "SKU-101", "Север") == (12, 0, 12)
    assert transit(client, "SKU-101")["FBS"] == 3

    tr = client.post(
        "/api/transfers", json={"source_id": n, "dest_id": y, "product_id": p, "quantity": 4}
    )
    assert tr.status_code == 201
    assert cell(client, "SKU-101", "Север") == (8, 0, 8)
    assert cell(client, "SKU-101", "Юг") == (6, 0, 6)  # у получателя до приёмки ничего
    assert transit(client, "SKU-101")["TRANSFER"] == 4
    assert client.post(f"/api/transfers/{tr.json()['id']}/receive").status_code == 200
    assert cell(client, "SKU-101", "Юг") == (10, 0, 10)
    assert transit(client, "SKU-101")["TRANSFER"] == 0

    fbo = client.post("/api/fbo", json={"warehouse_id": n, "product_id": p, "quantity": 3})
    assert fbo.status_code == 201
    assert cell(client, "SKU-101", "Север") == (5, 0, 5)
    assert transit(client, "SKU-101")["FBO"] == 3
    assert client.post(f"/api/fbo/{fbo.json()['id']}/receive").status_code == 200
    assert transit(client, "SKU-101")["FBO"] == 0
    assert_consistent(session)

    assert client.get("/api/operations/open").json()[0]["type"] == "FBS"  # у клиента, в пути


def test_error_format_and_codes(client, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    cmd = {"warehouse_id": n, "product_id": p}

    client.post("/api/fbs/reservations", json={**cmd, "quantity": 8})
    short = client.post("/api/fbs/reservations", json={**cmd, "quantity": 5})
    assert short.status_code == 409
    body = short.json()["error"]
    assert body["code"] == "insufficient_stock"
    assert "доступно 2" in body["message"] and "запрошено 5" in body["message"]
    assert "Юг 6" in body["message"]

    missing = client.post("/api/receipts", json={**cmd, "product_id": 9999, "quantity": 1})
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")

    zero = client.post("/api/receipts", json={**cmd, "quantity": 0})
    assert (zero.status_code, zero.json()["error"]["code"]) == (422, "validation_error")

    for bad in ("5", 5.5, True, None):  # строгая типизация на входе
        res = client.post("/api/receipts", json={**cmd, "quantity": bad})
        assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error"
    assert "Некорректные данные" in res.json()["error"]["message"]


def test_double_confirmation_over_http(client, session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = client.post(
        "/api/fbs/reservations", json={"warehouse_id": n, "product_id": p, "quantity": 3}
    ).json()

    assert client.post(f"/api/fbs/{op['id']}/ship").status_code == 200
    again = client.post(f"/api/fbs/{op['id']}/ship")
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"
    assert cell(client, "SKU-101", "Север") == (7, 0, 7)  # списано один раз
    assert transit(client, "SKU-101")["FBS"] == 3
    assert_consistent(session)


def test_warehouse_endpoints_and_archive_keep_stock_visible(client, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]

    created = client.post("/api/warehouses", json={"name": "Запад"})
    assert created.status_code == 201 and created.json()["archived"] is False
    dup = client.post("/api/warehouses", json={"name": "Запад"})
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "conflict")
    renamed = client.patch(f"/api/warehouses/{created.json()['id']}", json={"name": "Запад-2"})
    assert renamed.json()["name"] == "Запад-2"

    res = client.post(f"/api/warehouses/{y}/archive")
    assert res.status_code == 200 and res.json()["archived"] is True
    data = client.get("/api/stock").json()
    south = next(w for w in data["warehouses"] if w["name"] == "Юг")
    assert south["archived"] is True  # остаток виден, склад помечен
    assert cell(client, "SKU-101", "Юг") == (6, 0, 6)
    assert "Запад-2" in [w["name"] for w in data["warehouses"]]  # пустой активный склад виден

    blocked = client.post("/api/receipts", json={"warehouse_id": y, "product_id": p, "quantity": 1})
    assert blocked.status_code == 409 and "в архиве" in blocked.json()["error"]["message"]

    client.post("/api/fbs/reservations", json={"warehouse_id": n, "product_id": p, "quantity": 1})
    refused = client.post(f"/api/warehouses/{n}/archive")
    assert refused.status_code == 409 and "активных резервов" in refused.json()["error"]["message"]


def test_history_endpoint_has_names_and_filter(client, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    client.post(
        "/api/transfers", json={"source_id": n, "dest_id": y, "product_id": p, "quantity": 4}
    )
    top = client.get("/api/history", params={"warehouse_id": y}).json()[0]
    assert (top["type"], top["sku"], top["quantity"]) == ("SHIP_TRANSFER", "SKU-101", 4)
    assert (top["from_warehouse"], top["to_warehouse"]) == ("Север", "Юг")
    assert top["operation_status"] == "IN_TRANSIT"
    assert client.get("/api/history", params={"limit": 0}).status_code == 422


def test_stock_endpoint_runs_constant_number_of_queries(client, session, engine):
    for i in range(4):
        create_warehouse(session, f"Доп-{i}")
    statements = []

    def count(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert client.get("/api/stock").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 4  # склады, товары, остатки, «в пути» — независимо от объёма


class FakeDriverError(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "status", "code"),
    [
        ("40P01", 409, "concurrent_conflict"),
        ("40001", 409, "concurrent_conflict"),
        ("23514", 500, "internal_error"),
    ],
)
def test_database_errors_become_json_not_stack_traces(
    client, ids, monkeypatch, sqlstate, status, code
):
    def boom(*args, **kwargs):
        raise OperationalError("INSERT ...", {}, FakeDriverError(sqlstate))

    monkeypatch.setattr("app.api.routes.receive", boom)
    res = client.post(
        "/api/receipts",
        json={"warehouse_id": ids.w["Север"], "product_id": ids.p["SKU-101"], "quantity": 1},
    )
    assert res.status_code == status
    assert res.json()["error"]["code"] == code
