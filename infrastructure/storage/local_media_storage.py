from __future__ import annotations

import os
import shutil
from pathlib import Path

from domain.storage import MediaStorageService


class LocalFilesystemMediaStorage(MediaStorageService):
    """Temporary local adapter used before OSS integration."""

    def __init__(self, temp_root: str | None = None, output_root: str | None = None) -> None:
        project_root = Path.cwd()
        default_temp_root = project_root / "data" / "media" / "temp"
        default_output_root = project_root / "data" / "media" / "output"
        self.temp_root = temp_root or os.getenv("MEDIA_TEMP_ROOT", str(default_temp_root))
        self.output_root = output_root or os.getenv("MEDIA_OUTPUT_ROOT", str(default_output_root))

    def prepare_task_workspace(self, task_id: str) -> str:
        task_dir = os.path.join(self.temp_root, task_id)
        os.makedirs(task_dir, exist_ok=True)
        return task_dir

    def resolve_temp_file(self, task_id: str, filename: str) -> str:
        return os.path.join(self.temp_root, task_id, filename)

    def resolve_final_output(self, task_id: str) -> str:
        os.makedirs(self.output_root, exist_ok=True)
        return os.path.join(self.output_root, f"MV_{task_id}.mp4")

    def cleanup_task_workspace(self, task_id: str) -> None:
        task_dir = os.path.join(self.temp_root, task_id)
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir)
