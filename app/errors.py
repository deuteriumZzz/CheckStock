class DomainError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class InsufficientStock(Conflict):
    code = "insufficient_stock"
    """Нехватка на выбранном складе; в сообщении — остатки на других ФФ (подсказка менеджеру)."""
