from __future__ import annotations

from pathlib import Path
from typing import Protocol


class MediaStorage(Protocol):
    def ensure_task_workspace(self, task_id: str) -> Path:
        ...
