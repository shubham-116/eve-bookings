"""Small shared helpers: errors, validation, pagination, transactions, time."""
import re
from contextlib import contextmanager
from datetime import datetime, timezone


class ApiError(Exception):
    """Raise anywhere; the app turns it into a JSON error response."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TYPE_NAMES = {int: "an integer", str: "a string"}


def validate(data, required, optional=None):
    """Check a JSON body against {field: type}. Returns a cleaned dict (strings stripped)."""
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_body", "Request body must be a JSON object")
    out = {}
    for field, typ in {**required, **(optional or {})}.items():
        value = data.get(field)
        if value is None:
            if field in required:
                raise ApiError(400, "validation_error", f"'{field}' is required")
            continue
        is_int = isinstance(value, int) and not isinstance(value, bool)  # bool is an int in Python
        if (typ is int and not is_int) or (typ is str and not isinstance(value, str)):
            raise ApiError(400, "validation_error", f"'{field}' must be {_TYPE_NAMES[typ]}")
        if typ is str:
            value = value.strip()
            if not value:
                raise ApiError(400, "validation_error", f"'{field}' must not be empty")
        out[field] = value
    return out


def page_params(args):
    """?page=1&page_size=20 -> (page, size, offset). size is capped at 100."""
    try:
        page, size = int(args.get("page", 1)), int(args.get("page_size", 20))
    except ValueError:
        raise ApiError(400, "validation_error", "'page' and 'page_size' must be integers")
    if page < 1 or not 1 <= size <= 100:
        raise ApiError(400, "validation_error", "page >= 1 and 1 <= page_size <= 100")
    return page, size, (page - 1) * size


@contextmanager
def tx(db):
    """One atomic write transaction. BEGIN IMMEDIATE takes the write lock up front,
    so concurrent writers queue instead of interleaving."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
