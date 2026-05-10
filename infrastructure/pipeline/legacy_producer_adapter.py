from __future__ import annotations

from typing import Callable, Protocol


class ProducerBackend(Protocol):
    temp_dir: str

    def download_step(self, song_name: str, output_path: str):
        ...

    def transcribe_step(self, video_ref, audio_path: str):
        ...

    def audit_step(self, english_texts: list[str], *, title: str = "", references: list[dict] | None = None) -> dict:
        ...

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        ...

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        ...


class MissingProducerBackend:
    temp_dir = ""

    def _raise(self):
        raise RuntimeError(
            "HipHop producer backend is unavailable. Expected core.hipHopProducer.HipHopAutoProject."
        )

    def download_step(self, song_name: str, output_path: str):
        self._raise()

    def transcribe_step(self, video_ref, audio_path: str):
        self._raise()

    def audit_step(self, english_texts: list[str], *, title: str = "", references: list[dict] | None = None) -> dict:
        self._raise()

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        self._raise()

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        self._raise()


def create_default_producer_backend() -> ProducerBackend:
    try:
        from core.hipHopProducer import HipHopAutoProject  # type: ignore

        return HipHopAutoProject()
    except Exception:
        return MissingProducerBackend()


ProducerBackendFactory = Callable[[], ProducerBackend]
