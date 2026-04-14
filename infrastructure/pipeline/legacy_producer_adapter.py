from __future__ import annotations


class LegacyProducerBackend:
    def run(self, task_id: str, song_name: str) -> None:
        return None


def create_default_producer_backend() -> LegacyProducerBackend:
    return LegacyProducerBackend()
