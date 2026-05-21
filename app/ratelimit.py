import time
from collections import defaultdict
from threading import Lock

_lock = Lock()
_attempts: dict[str, list[float]] = defaultdict(list)

WINDOW = 60   # seconds
MAX_ATTEMPTS = 10


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        _attempts[ip] = [t for t in _attempts[ip] if now - t < WINDOW]
        if len(_attempts[ip]) >= MAX_ATTEMPTS:
            return True
        _attempts[ip].append(now)
        return False
