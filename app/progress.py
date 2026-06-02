import copy
import threading

_lock = threading.Lock()
_state: dict = {
    "indexing": {
        "running": False,
        "phase": "",
        "current_drive": "",
        "drives_done": 0,
        "drives_total": 0,
        "files_done": 0,
        "last_finished_at": None,
        "last_message": None,
        "last_ok": None,
    },
    "enrichment": {
        "running": False,
        "done": 0,
        "total": 0,
        "last_finished_at": None,
        "last_message": None,
        "last_ok": None,
    },
    "auto_describe": {
        "running": False,
        "done": 0,
        "total": 0,
        "last_finished_at": None,
        "last_message": None,
        "last_ok": None,
    },
}


def update(section: str, **kwargs) -> None:
    with _lock:
        _state[section].update(kwargs)


def snapshot() -> dict:
    with _lock:
        return copy.deepcopy(_state)
