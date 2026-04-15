from __future__ import annotations

from abc import ABC, abstractmethod


class MediaStorageService(ABC):
    @abstractmethod
    def prepare_task_workspace(self, task_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def resolve_temp_file(self, task_id: str, filename: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def resolve_final_output(self, task_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def cleanup_task_workspace(self, task_id: str) -> None:
        raise NotImplementedError
