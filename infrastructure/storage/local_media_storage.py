from __future__ import annotations

import os
from pathlib import Path


class LocalFilesystemMediaStorage:
    def __init__(self, root: str | None = None) -> None:
        configured_root = root or os.getenv("MEDIA_OUTPUT_ROOT") or "data/runtime-media"
        self._root = Path(configured_root)

    def ensure_task_workspace(self, task_id: str) -> Path:
        workspace = self._root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace
