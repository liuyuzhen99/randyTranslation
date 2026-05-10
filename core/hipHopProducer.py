from __future__ import annotations

import gc
import json
import os
import re
import shutil
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

        logger.info("开始下载视频: %s", song_name)
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
        ydl_opts.update(self._cookie_options())
        ydl_opts.update(self._javascript_challenge_options())
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            status = ydl.download([song_name])
        if status != 0 or not os.path.exists(output_path):
            raise RuntimeError(f"video download failed for {song_name}")
        logger.info("视频下载完成: %s", output_path)
        return output_path

    def _cookie_options(self) -> dict:
        cookie_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
        if cookie_file:
            return {"cookiefile": cookie_file}

        browser_cookie_source = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
        if not browser_cookie_source:
            return {}

        parts = [part.strip() for part in browser_cookie_source.split(":") if part.strip()]
        if not parts:
            return {}
        logger.info("yt-dlp will load cookies from browser source: %s", browser_cookie_source)
        return {"cookiesfrombrowser": tuple(parts)}

    def _javascript_challenge_options(self) -> dict:
        options: dict = {}
        runtimes = self._parse_js_runtimes(os.getenv("YTDLP_JS_RUNTIMES", ""))
        if runtimes:
            logger.info("yt-dlp will enable JavaScript runtimes: %s", ", ".join(runtimes.keys()))
            options["js_runtimes"] = runtimes

        remote_components = [
            component.strip()
            for component in os.getenv("YTDLP_REMOTE_COMPONENTS", "").split(",")
            if component.strip()
        ]
        if remote_components:
            logger.info(
                "yt-dlp will allow remote JavaScript components: %s",
                ", ".join(remote_components),
            )
            options["remote_components"] = remote_components
        return options

    @staticmethod
    def _parse_js_runtimes(value: str) -> dict:
        runtimes = {}
        for raw_runtime in value.split(","):
            runtime = raw_runtime.strip()
            if not runtime:
                continue
            name, separator, path = runtime.partition(":")
            name = name.strip().lower()
            if not name:
                continue
            config = {}
            if separator and path.strip():
                config["path"] = path.strip()
            runtimes[name] = config
        return runtimes

    def transcribe_step(self, video_ref, audio_path: str):
        logger.info("开始抽取音频并转写: %s", video_ref)
        self._extract_audio(video_ref, audio_path)
        transcription_audio = self._separate_vocals(audio_path)
        if self._whisper is None:
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(
                os.getenv("WHISPER_MODEL", "large-v3-turbo"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            )
        segments_iter, _ = self._whisper.transcribe(
            transcription_audio,
            initial_prompt="Rap, Hip-hop, lyrics, slang.",
            beam_size=5,
            best_of=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
            word_timestamps=True,
        )
        segments = []
        for segment in segments_iter:
            text = segment.text.strip()
            if len(text) <= 1:
                continue
            segments.append({"start": round(float(segment.start), 2), "end": round(float(segment.end), 2), "text": text})
        if not segments:
            raise RuntimeError("transcription produced no lyric segments")

        reviewed_segments = self._review_transcription_segments(segments)
        english_texts = []
        for segment in reviewed_segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            english_texts.append(text)
        if not reviewed_segments or not english_texts:
            raise RuntimeError("transcription produced no lyric segments")
        logger.info("音频转写完成: segments=%s", len(reviewed_segments))
        return reviewed_segments, english_texts

    def audit_step(self, english_texts: list[str], *, title: str = "", references: list[dict] | None = None) -> dict:
        logger.info("开始 AI 审计: title=%s references=%s", title or "Unknown Title", len(references or []))
        lyrics = "\n".join(text.strip() for text in english_texts if text and text.strip())
        if len(lyrics) < 10:
            return {"score": 0, "decision": "Reject", "reason": "歌词内容不足以审计", "key_lyrics": []}

        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
        if not api_key or not base_url:
            raise RuntimeError("Audit API is not configured.")

        from openai import OpenAI

        reference_str = self._format_audit_references(references or [])
        prompt = f"""你是一位资深的 Hip-Hop 乐评人，审美标准深度对齐 J. Cole, Kendrick Lamar 和 Dave。
你的任务是审计下方由 Whisper 识别出的原始歌词。

审计维度（打分 0-10）：
1. 叙事深度 (Storytelling)：是否在讲述一个连贯、深刻的人生故事或社会现象？
2. 词作技巧 (Lyricism)：是否有复杂的押韵、隐喻或双关？
3. 情感共鸣 (Vibe)：是否传递出沉思、奋斗、忧郁或清醒的情绪？
4. 负面剔除 (Red Flags)：如果充满无意义的重复、炫富、纯粹的暴力，请大幅扣分。

要求：基于我库中已有的高审美标准（参考下方案例），审计这首新歌。
歌曲标题：{title or "Unknown Title"}
{reference_str}

待审计歌词：
{lyrics[:3000]}

请直接输出 JSON 格式：
{{
  "score": 0-100,
  "reason": "简短的中文评价",
  "key_lyrics": ["提取2-3句最灵魂的英文原文"],
  "decision": "Pass/Reject"
}}"""
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_AUDIT_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "You are a professional music critic."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        try:
            audit_json = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Audit response could not be parsed: {exc}") from exc
        logger.info(
            "AI 审计完成: score=%s decision=%s",
            audit_json.get("score"),
            audit_json.get("decision", "Reject"),
        )
        return {
            "score": self._coerce_score(audit_json.get("score")),
            "decision": str(audit_json.get("decision", "Reject")),
            "reason": str(audit_json.get("reason", "")),
            "key_lyrics": [
                str(item)
                for item in audit_json.get("key_lyrics", [])
                if isinstance(item, str)
            ][:3],
        }

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        logger.info("开始生成双语字幕: lines=%s output=%s", len(english_texts), output_file)
        translations = self._translate_lines(english_texts)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as file_obj:
            for index, segment in enumerate(segments, start=1):
                english = segment["text"]
                chinese = translations[index - 1] if index - 1 < len(translations) else english
                file_obj.write(f"{index}\n")
                file_obj.write(f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}\n")
                file_obj.write(f"{english}\n{chinese}\n\n")
        logger.info("双语字幕生成完成: %s", output_file)
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        logger.info("开始视频压制: video=%s srt=%s output=%s", video_ref, srt_file, final_path)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        ffmpeg = self._resolve_ffmpeg_binary()
        logger.info("使用 ffmpeg 二进制: %s", ffmpeg)
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
            ffmpeg,
            "-i",
            video_ref,
            "-vf",
            f"subtitles='{self._escape_ffmpeg_path(srt_file)}':force_style='{self._escape_ffmpeg_style(style)}'",
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
        if not self._supports_subtitles_filter(ffmpeg):
            raise RuntimeError(
                "ffmpeg subtitles filter is unavailable for "
                f"{ffmpeg}. Install/use ffmpeg-full or another build with libass enabled."
            )
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if self._is_missing_subtitles_filter(result.stderr):
                raise RuntimeError(
                    "ffmpeg subtitles filter is unavailable for "
                    f"{ffmpeg}. Install/use ffmpeg-full or another build with libass enabled."
                )
            raise RuntimeError(f"ffmpeg render failed: {result.stderr[-500:]}")
        logger.info("视频压制成功: %s", final_path)
        return final_path

    def _mux_subtitle_track(self, video_ref: str, srt_file: str, final_path: str):
        ffmpeg = self._resolve_ffmpeg_binary()
        cmd = [
            ffmpeg,
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
        logger.info("SRT mux fallback 完成，字幕已作为 MP4 字幕轨写入: %s", final_path)
        return final_path

    def _extract_audio(self, video_ref: str, audio_path: str) -> None:
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        cmd = [self._resolve_ffmpeg_binary(), "-i", video_ref, "-vn", "-y", audio_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr[-500:]}")
        logger.info("音频抽取完成: %s", audio_path)

    def _separate_vocals(self, audio_path: str) -> str:
        vocals_path = os.path.join(os.path.dirname(audio_path), "vocals.wav")
        try:
            import torch
            import torchaudio
            from demucs.apply import apply_model
            from demucs import pretrained
        except Exception as exc:
            logger.warning("Demucs dependencies are unavailable; transcribing raw audio. error=%s", exc)
            return audio_path

        try:
            model_name = os.getenv("DEMUCS_MODEL", "hdemucs_mmi")
            model = pretrained.get_model(model_name)
            model.eval()
            device = torch.device(os.getenv("DEMUCS_DEVICE", "") or ("mps" if torch.backends.mps.is_available() else "cpu"))
            model.to(device)
            wav, sample_rate = torchaudio.load(audio_path)
            if sample_rate != model.samplerate:
                wav = torchaudio.functional.resample(wav, sample_rate, model.samplerate)
            wav = wav.to(device).unsqueeze(0)
            with torch.no_grad():
                sources = apply_model(
                    model,
                    wav,
                    device=device,
                    shifts=int(os.getenv("DEMUCS_SHIFTS", "2")),
                    split=True,
                    overlap=float(os.getenv("DEMUCS_OVERLAP", "0.1")),
                )
            separated = dict(zip(model.sources, sources.squeeze(0)))
            vocals = separated.get("vocals")
            if vocals is None:
                logger.warning("Demucs did not return a vocals stem; transcribing raw audio.")
                return audio_path
            torchaudio.save(vocals_path, vocals.cpu(), model.samplerate)
            return vocals_path
        except Exception as exc:
            logger.warning("Demucs vocal separation failed; transcribing raw audio. error=%s", exc)
            return audio_path
        finally:
            try:
                del wav, sources, separated
            except Exception:
                pass
            try:
                if "torch" in locals() and torch.backends.mps.is_available():
                    torch.mps.empty_cache()
            except Exception:
                pass
            gc.collect()

    def _review_transcription_segments(self, segments: list[dict]) -> list[dict]:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
        if not api_key or not base_url:
            return segments
        try:
            from core.aiReviewer import MusicReviewer

            reviewer = MusicReviewer(api_key=api_key, base_url=base_url)
            reviewed = reviewer.audit_transcription_segments(segments)
            return reviewed or segments
        except Exception as exc:
            logger.warning("AI transcription review failed; keeping raw segments. error=%s", exc)
            return segments

    def _translate_lines(self, english_texts: list[str]) -> list[str]:
        logger.info("开始调用 AI 翻译歌词: lines=%s", len(english_texts))
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
        if not api_key or not base_url:
            raise RuntimeError("Translation API is not configured.")

        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        anchored_block = self._prepare_anchored_lyrics(english_texts)
        prompt = f"""你是一个专业的 Hip-hop 中文翻译官，能够准确理解并翻译嘻哈、R&B音乐，准确且地道不突兀，十分吸引听众。请将以下歌词翻译成中文。

要求：
【硬性要求，必须遵守！！！】你必须严格遵守 1:1 映射。
【硬性要求，必须遵守！！！】输入是 <L52>...</L52>，输出就必须截止于 <R52>...</R52>。
【硬性要求，必须遵守！！！】严禁生成任何不在输入列表中的索引号。每一行 <Rn> 必须是对 <Ln> 内容的完整且唯一的翻译。
- 逐行翻译下方 XML 标签内的内容。
- 必须以 <R{{i}}>中文翻译</R{{i}}> 的格式返回。
- 严禁合并行。
- 保持行数和序号一一对应。
- 翻译要地道，保留歌词原本的俚语和韵味。
- 只返回翻译后的中文，不要包含任何解释。
- 语义通顺，能从全文的角度理解上下文含义。
- 根据歌词的内容设定语境。
- 不含脏话，敏感词汇要进行隐晦处理。

【参考示例】:
{self._translation_fallback_examples()}

歌词列表：
{anchored_block}

直接返回结果，不要任何开场白。"""
        response = client.chat.completions.create(
            model=os.getenv("DEEPSEEK_TRANSLATION_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "You are a lyric synchronization expert."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content or "[]"
        try:
            parsed = self._parse_translation_response(content)
        except ValueError as exc:
            raise RuntimeError(f"Translation response could not be parsed: {exc}") from exc
        if not isinstance(parsed, list):
            raise RuntimeError("Translation response was not a line list.")
        translations = [str(item).strip() for item in parsed]
        if len(translations) != len(english_texts):
            raise RuntimeError(
                f"Translation response line count mismatch: expected {len(english_texts)}, got {len(translations)}."
            )
        reviewed = self._review_translation_lines(english_texts, translations)
        logger.info("AI 歌词翻译完成: lines=%s", len(reviewed))
        return reviewed

    @staticmethod
    def _prepare_anchored_lyrics(english_texts: list[str]) -> str:
        return "".join(f"<L{index}>{text}</L{index}>\n" for index, text in enumerate(english_texts, start=1))

    @staticmethod
    def _translation_fallback_examples() -> str:
        return """
Example 1 (Metaphor & Vibe):
Input: <L1>Open beaks in the nest, money machines</L1>
Output: <R1>巢中鸟喙饥渴，金钱推动运作</R1>

Example 2 (Storytelling & Emotion):
Input: <L2>This life too beautiful to hide what I'm feeling</L2>
Output: <R2>这生活美好得无法掩藏我的感受</R2>

Example 3 (Slang & Social Critique):
Input: <L3>How the fuck is it that so many cops are dirty? (Huh?)</L3>
Output: <R3>怎么他妈有那么多黑条子？</R3>
"""

    def _review_translation_lines(self, english_texts: list[str], translations: list[str]) -> list[str]:
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip()
        if not api_key or not base_url:
            return translations
        try:
            from core.aiReviewer import MusicReviewer

            virtual_blocks = []
            for index, english in enumerate(english_texts, start=1):
                current_zh = translations[index - 1]
                virtual_blocks.append(
                    f"[[BLOCK_ID_{index}]]\nSOURCE_EN: {english}\nCURRENT_ZH: {current_zh}"
                )
            reviewer = MusicReviewer(api_key=api_key, base_url=base_url)
            adjustments = reviewer.audit_translation_map("\n\n".join(virtual_blocks))
        except Exception as exc:
            logger.warning("AI translation review failed; keeping initial translations. error=%s", exc)
            return translations
        if not isinstance(adjustments, list):
            logger.warning("AI translation review returned invalid adjustments: %s", type(adjustments))
            return translations

        reviewed = list(translations)
        for adjustment in adjustments:
            if not isinstance(adjustment, dict):
                continue
            if "index" not in adjustment or "fixed_zh" not in adjustment:
                continue
            try:
                index = int(adjustment["index"])
            except (TypeError, ValueError):
                continue
            if index < 1 or index > len(reviewed):
                continue
            old_text = reviewed[index - 1].strip()
            new_text = str(adjustment["fixed_zh"]).strip().replace("\n", " ")
            if not new_text or old_text == new_text:
                continue
            if len(new_text) > len(old_text) + 15 and len(new_text) > 30:
                logger.warning("Blocked suspicious translation review merge at line %s.", index)
                continue
            reviewed[index - 1] = new_text
        return reviewed

    @staticmethod
    def _format_audit_references(references: list[dict]) -> str:
        if not references:
            return ""
        lines = ["\n【参考库中已通过的高质量案例】:"] 
        for reference in references[:3]:
            title = reference.get("title") or reference.get("artist") or "Unknown"
            lyrics = reference.get("lyrics") or reference.get("text") or reference.get("document") or ""
            lines.append(f"- 歌曲《{title}》: {str(lyrics)[:300]}...")
        return "\n".join(lines)

    @staticmethod
    def _coerce_score(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_translation_response(content: str):
        text = content.strip()
        candidates = [text]

        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            candidates.append(fenced.group(1).strip())

        array_match = re.search(r"\[[\s\S]*\]", text)
        if array_match:
            candidates.append(array_match.group(0))

        object_match = re.search(r"\{[\s\S]*\}", text)
        if object_match:
            candidates.append(object_match.group(0))

        errors: list[str] = []
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                continue
            if isinstance(parsed, dict):
                for key in ("translations", "lines", "result", "data"):
                    value = parsed.get(key)
                    if isinstance(value, list):
                        return value
                numeric_items = sorted(
                    (
                        (int(key), value)
                        for key, value in parsed.items()
                        if str(key).isdigit()
                    ),
                    key=lambda item: item[0],
                )
                if numeric_items:
                    return [value for _key, value in numeric_items]
            return parsed

        tagged_lines = [
            text.strip()
            for _index, text in sorted(
                (
                    (int(index), value)
                    for index, value in re.findall(r"<R(\d+)>(.*?)</R\1>", text, flags=re.DOTALL)
                ),
                key=lambda item: item[0],
            )
        ]
        if tagged_lines:
            return tagged_lines

        numbered_lines = []
        for line in text.splitlines():
            match = re.match(r"^\s*(?:\d+[\.\)、)]|[-*])\s*(.+?)\s*$", line)
            if match:
                numbered_lines.append(match.group(1))
        if numbered_lines:
            return numbered_lines
        raise ValueError(errors[-1] if errors else "empty response")

    @staticmethod
    def _resolve_ffmpeg_binary() -> str:
        configured = os.getenv("FFMPEG_BINARY", "").strip()
        if configured:
            return configured

        for candidate in (
            "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            shutil.which("ffmpeg"),
            "/usr/bin/ffmpeg",
        ):
            if candidate and os.path.exists(candidate):
                return candidate
        return "ffmpeg"

    @staticmethod
    def _is_missing_subtitles_filter(stderr: str) -> bool:
        return bool(
            re.search(r"No such filter:\s*'?subtitles'?", stderr)
            or "Unknown filter 'subtitles'" in stderr
            or "No option name near" in stderr and "subtitles=" in stderr
        )

    @staticmethod
    def _supports_subtitles_filter(ffmpeg: str) -> bool:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-h", "filter=subtitles"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            logger.warning("Unable to inspect ffmpeg subtitles filter support: %s", exc)
            return False
        output = f"{result.stdout}\n{result.stderr}"
        return "Unknown filter 'subtitles'" not in output and "AVOptions" in output

    @staticmethod
    def _escape_ffmpeg_path(path: str) -> str:
        return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    @staticmethod
    def _escape_ffmpeg_style(style: str) -> str:
        return style.replace("\\", "\\\\").replace("'", "\\'")
