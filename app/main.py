from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import DBAPIError

from app.api.routes import router
from app.errors import DomainError

BASE = Path(__file__).parent

app = FastAPI(title="CheckStock", description="Где сейчас товар: остатки, резервы и движения ФФ")
app.include_router(router)


class RevalidatedStatic(StaticFiles):
    """Браузер каждый раз сверяет файл по ETag: после правки интерфейса не остаётся старого кеша."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStatic(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """Единый формат ошибок: {"error": {"code": ..., "message": ...}}."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(DomainError)
def domain_error_handler(_: Request, exc: DomainError):
    return error_response(exc.status_code, exc.code, exc.message)


# PostgreSQL: deadlock, serialization failure, lock not available — операцию можно просто повторить.
CONCURRENCY_SQLSTATES = {"40P01", "40001", "55P03"}


@app.exception_handler(DBAPIError)
def db_error_handler(_: Request, exc: DBAPIError):
    # Транзакция уже откатилась (сессия закрывается без commit): данные не изменены.
    if getattr(exc.orig, "sqlstate", None) in CONCURRENCY_SQLSTATES:
        return error_response(
            409, "concurrent_conflict", "Операции пересеклись во времени. Повторите действие."
        )
    return error_response(500, "internal_error", "Внутренняя ошибка сервера. Данные не изменены.")


@app.exception_handler(RequestValidationError)
def validation_error_handler(_: Request, exc: RequestValidationError):
    details = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
    return error_response(422, "validation_error", f"Некорректные данные запроса — {details}")


@app.get("/", include_in_schema=False)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")
