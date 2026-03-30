import gc
import librosa
import numpy as np
import torch
import torchaudio
from faster_whisper import WhisperModel
from demucs import pretrained
from demucs.apply import apply_model
from demucs.pretrained import get_model
from demucs.apply import apply_model
import torch
import os
import traceback
from dbm import sqlite3
from utils.logger_manager import log_manager

# 初始化专门针对转录任务的 Logger
logger = log_manager.get_task_logger("TRANSCRIBER")

class SeparateTranscriber:
    def __init__(self):
        logger.info("🏗️ 正在初始化转录核心模型 (Whisper Medium + Demucs)...")
        try:
            self.whisper = WhisperModel("medium", device="cpu", compute_type="int8")
            # model = pretrained.get_model("htdemucs_ft")   # 推荐：人声效果最好
            self.model = pretrained.get_model("htdemucs")    # 如果想更快，改成这个
            self.model.eval()
            self.device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
            self.model.to(self.device)
            logger.info(f"✅ 模型加载完成。Demucs 运行设备: {self.device}")
        except Exception as e:
            logger.error(f"🚨 模型初始化失败: {e}")
            logger.error(traceback.format_exc())
            raise
        

    def transcribe_step(self, video_id, db_path, task_queue):
        # --------------------------
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT title FROM videos WHERE video_id=?", (video_id,))
            video_info = cur.fetchall()
            title = video_info[0][0] if video_info else "Unknown Title"
            logger.info(f"✅ 成功从数据库读取歌曲:{title} video_id:{video_id} ")
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库video信息: {db_err}")
            logger.error(traceback.format_exc())
            return [], []
        audio_path = f"/Users/randy/Downloads/temp/{title}_{video_id}.mp3"  # 这里的路径要和下载模块保持一致
        vocals_save_path = os.path.join('/Users/randy/Downloads/temp/', "vocals.wav")
        try:
            if not os.path.exists(audio_path):
                logger.error(f"❌ 原始音频文件不存在: {audio_path}")
                return [], []
            logger.info(f"🎤 正在读取音频并重采样: {os.path.basename(audio_path)}")
            wav, sr = torchaudio.load(audio_path)
            # 重采样到模型要求的 44100Hz
            if sr != self.model.samplerate:
                logger.info(f"🔄 采样率不匹配 ({sr}Hz -> {self.model.samplerate}Hz)，执行重采样...")
                wav = torchaudio.functional.resample(wav, sr, self.model.samplerate)

            wav = wav.to(self.device).unsqueeze(0)   # 添加 batch 维度
            logger.info("⚡ 执行 Demucs 人声分离 (htdemucs)...")
            with torch.no_grad():
                sources = apply_model(
                    self.model,
                    wav,
                    device=self.device,
                    shifts=2,          # 可改成 1（更快）或 4（更好）
                    split=True,        # 分块处理，省显存
                    overlap=0.1
                )
            # sources.shape: [1, num_stems, channels, time]
            sources = sources.squeeze(0)        # 去掉 batch 维度
            stems = self.model.sources
            separated = dict(zip(stems, sources))
            vocals_tensor = separated["vocals"]
            torchaudio.save(vocals_save_path, vocals_tensor.cpu(), self.model.samplerate)
            # 关键：及时清理显存/内存
            del wav, sources, separated
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            gc.collect()

            # --- B. Faster-Whisper 转录 (带幻听控制) ---
            logger.info("🎙️ 正在执行识别 (Faster-Whisper)...")
            segments, _ = self.whisper.transcribe(
                vocals_save_path,
                initial_prompt="Rap, Hip-hop, lyrics, slang.", 
                beam_size=5,
                best_of=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                # 稳定性核心：关闭上下文关联，防止 Rap 的重复节奏导致“幻听循环”
                condition_on_previous_text=False,
                compression_ratio_threshold=2.4, 
                no_speech_threshold=0.6,
                word_timestamps=True
            )
            
            full_data = []
            english_texts_only = []

            for segment in segments:
                clean_text = segment.text.strip()
                # 过滤掉单字符的无效识别
                if len(clean_text) > 1:
                    full_data.append({
                        'start': round(float(segment.start), 2),
                        'end': round(float(segment.end), 2),
                        'text': clean_text
                    })
                    english_texts_only.append(clean_text)
            logger.info(f"✅ 转录完成，共识别出 {len(english_texts_only)} 条字幕。")
            raw_lyrics = "\n".join(english_texts_only)
            task_queue.put(("UPDATE_LYRICS", {
                'video_id': video_id,
                'lyrics': raw_lyrics
            }))
            logger.info("✅ 已将更新歌词任务推送到数据库队列。")
            # --- C. 清理临时文件 ---
            # 仅清理分离后的人声 wav，原始音频建议在主循环最后处理
            os.remove(vocals_save_path)        
            return full_data, english_texts_only
        except Exception as e:
            logger.error(f"🚨 转录流程发生异常: {e}")
            logger.error(traceback.format_exc())
            # 发生异常也要清理临时人声文件，防止磁盘写满
            if os.path.exists(vocals_save_path):
                os.remove(vocals_save_path)
            return [], []
        
    def analyze_audio(self,video_id, db_path, task_queue, lyrics_text):
        logger.info(f"📊 开始分析音频: {video_id}")
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT title FROM videos WHERE video_id=?", (video_id,))
            video_info = cur.fetchall()
            title = video_info[0][0] if video_info else "Unknown Title"
            logger.info(f"✅ 成功从数据库读取歌曲:{title} video_id:{video_id} ")
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库video信息: {db_err}")
            logger.error(traceback.format_exc())
            return None
        audio_path = f"/Users/randy/Downloads/temp/{title}_{video_id}.mp3"  # 这里的路径要和下载模块保持一致
        # 优化 1：指定采样率 sr=22050 (足够分析节奏)，并仅加载前 180 秒以节省 CPU 内存
        # 如果歌曲短于 180s，它会自动加载全长
        if not os.path.exists(audio_path):
            logger.error(f"❌ 音频文件不存在: {audio_path}")
            return None
        try:
            y, sr = librosa.load(audio_path, sr=22050, duration=180) 
            
            # 优化 2：计算节奏 (BPM)
            # 使用更轻量的运算方式
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sr)[0]
            
            # 优化 3：计算均方根能量 (RMS Energy)
            # 我们只取有声音部分的平均能量，避开片头片尾的静音
            rms = librosa.feature.rms(y=y)
            energy = np.mean(rms)
            
            # 优化 4：计算音频时长 (Duration)
            duration_seconds = librosa.get_duration(y=y, sr=sr)
            duration_minutes = duration_seconds / 60
            
            # 优化 5：计算词密度 (Words Per Minute)
            word_density = None
            if lyrics_text:
                # 简单的分词：按空格和标点符号分割
                word_count = len(lyrics_text.split())
                word_density = word_count / duration_minutes if duration_minutes > 0 else 0
            
            audit_results = {
                "tempo": round(tempo, 2),
                "energy": round(energy, 4),
                "word_density": round(word_density, 2)
            }
            logger.info(f"📈 分析结果: BPM={tempo:.1f}, Energy={energy:.4f}, WPM={word_density:.1f}")
            task_queue.put(("UPDATE_VIDEO_ANALYSIS", {
                'video_id': video_id,
                'title': title,
                'bpm': round(tempo, 2),
                'energy': round(energy, 4),
                'word_density': round(word_density, 2)
            }))
            logger.info("✅ 已将更新视频分析结果任务推送到数据库队列。")
            return audit_results
        except FileNotFoundError:
            logger.error(f"❌ 分析失败：找不到音频文件 {audio_path}")
        except Exception as e:
            logger.error(f"🚨 Librosa 物理分析发生未知错误: {e}")
            logger.error(traceback.format_exc())
        return None
    
# transcriber = SeparateTranscriber()
# full_data, english_texts_only = transcriber.transcribe_step("/Users/randy/Downloads/poor_thang_audio.mp3")
# print(english_texts_only)
