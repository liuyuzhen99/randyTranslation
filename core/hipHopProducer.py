from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from utils.logger_manager import log_manager

logger = log_manager.get_task_logger("PRODUCER")


def format_timestamp(seconds: float) -> str:
    millis = int((seconds - int(seconds)) * 1000)
    td_sec = int(seconds)
    td_min, td_sec = divmod(td_sec, 60)
    td_hour, td_min = divmod(td_min, 60)
    return f"{td_hour:02}:{td_min:02}:{td_sec:02},{millis:03}"


class HipHopAutoProject:
    """Producer backend used by PipelineOrchestrator for real render jobs."""

    def __init__(self) -> None:
        self.temp_dir = os.getenv("MEDIA_TEMP_ROOT", str(Path.cwd() / "data" / "media" / "temp"))
        self._whisper = None

    def download_step(self, song_name: str, output_path: str):
        import yt_dlp

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": output_path,
            "default_search": "ytsearch1:",
            "noplaylist": True,
            "merge_output_format": "mp4",
            "retries": 10,
            "fragment_retries": 10,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            status = ydl.download([song_name])
        if status != 0 or not os.path.exists(output_path):
            raise RuntimeError(f"video download failed for {song_name}")
        return output_path

    def transcribe_step(self, video_ref, audio_path: str):
        self._extract_audio(video_ref, audio_path)
        if self._whisper is None:
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(
                os.getenv("WHISPER_MODEL", "large-v3-turbo"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            )
        segments_iter, _ = self._whisper.transcribe(
            audio_path,
            initial_prompt="Rap, Hip-hop, lyrics, slang.",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
        )
        segments = []
        english_texts = []
        for segment in segments_iter:
            text = segment.text.strip()
            if not text:
                continue
            segments.append({"start": round(float(segment.start), 2), "end": round(float(segment.end), 2), "text": text})
            english_texts.append(text)
        if not segments:
            raise RuntimeError("transcription produced no lyric segments")
        return segments, english_texts

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        translations = self._translate_lines(english_texts)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as file_obj:
            for index, segment in enumerate(segments, start=1):
                english = segment["text"]
                chinese = translations[index - 1] if index - 1 < len(translations) else english
                file_obj.write(f"{index}\n")
                file_obj.write(f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}\n")
                file_obj.write(f"{english}\n{chinese}\n\n")
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        style = (
            "Fontname=PingFang SC,"
            "Fontsize=18,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "Outline=1,"
            "Shadow=0,"
            "Alignment=2,"
            "MarginV=25"
        )
        cmd = [
            "ffmpeg",
            "-i",
            video_ref,
            "-vf",
            f"subtitles=filename='{self._escape_ffmpeg_path(srt_file)}':force_style='{self._escape_ffmpeg_style(style)}'",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-y",
            final_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if "No such filter: 'subtitles'" in result.stderr:
                logger.warning("ffmpeg subtitles filter is unavailable; muxing SRT as an MP4 subtitle track.")
                return self._mux_subtitle_track(video_ref, srt_file, final_path)
            raise RuntimeError(f"ffmpeg render failed: {result.stderr[-500:]}")
        return final_path

    def _mux_subtitle_track(self, video_ref: str, srt_file: str, final_path: str):
        cmd = [
            "ffmpeg",
            "-i",
            video_ref,
            "-i",
            srt_file,
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
            "-y",
            final_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg subtitle mux fallback failed: {result.stderr[-500:]}")
        return final_path

    def _extract_audio(self, video_ref: str, audio_path: str) -> None:
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        cmd = ["ffmpeg", "-i", video_ref, "-vn", "-ac", "1", "-ar", "16000", "-y", audio_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[-500:]}")

    def _translate_lines(self, english_texts: list[str]) -> list[str]:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
        if not api_key or not base_url:
            logger.warning("Translation API is not configured; writing English-only fallback SRT.")
            return list(english_texts)

        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = "\n".join(f"{index}. {text}" for index, text in enumerate(english_texts, start=1))
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_TRANSLATION_MODEL", "deepseek-chat"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate hip-hop lyrics into Chinese. Return only a JSON array of strings, "
                        "with the same length and order as the input lines."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content or "[]"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Translation response was not JSON; writing English-only fallback SRT.")
            return list(english_texts)
        if not isinstance(parsed, list):
            return list(english_texts)
        return [str(item) for item in parsed]

    @staticmethod
    def _escape_ffmpeg_path(path: str) -> str:
        return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    @staticmethod
    def _escape_ffmpeg_style(style: str) -> str:
        return style.replace("\\", "\\\\").replace("'", "\\'").replace(",", "\\,")
