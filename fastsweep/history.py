"""Historique local des désinstallations et des nettoyages.

Local history of uninstalls and cleanups.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .system import app_data_dir

HISTORY_LIMIT = 1000


def operation_history_path() -> Path:
    return app_data_dir() / "history.json"


def load_operation_history() -> list[dict]:
    try:
        data = json.loads(operation_history_path().read_text(encoding="utf-8"))
        return [event for event in data if isinstance(event, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):  # fichier absent, JSON invalide ou encodage illisible / missing file, invalid JSON or unreadable encoding
        return []


def record_operation(app: str, operation: str, status: str, details: dict | None = None) -> None:
    path = operation_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    history = load_operation_history()
    history.append({
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": app,
        "operation": operation,
        "status": status,
        "details": details or {},
    })
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(history[-HISTORY_LIMIT:], ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
