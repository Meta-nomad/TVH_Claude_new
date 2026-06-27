import time
from typing import Any, Optional

_store: dict[str, tuple[Any, float]] = {}


def get(key: str) -> Optional[Any]:
    entry = _store.get(key)
    if not entry:
        return None
    value, exp = entry
    if time.time() > exp:
        del _store[key]
        return None
    return value


def set(key: str, value: Any, ttl: int) -> None:
    _store[key] = (value, time.time() + ttl)
