import yt_dlp
import os
import librosa
import numpy as np
import json
from faster_whisper import WhisperModel
from openai import OpenAI  # DeepSeek 兼容 OpenAI 格式

class MusicAuditor:
    def __init__(self, deepseek_api_key):
        # 初始化 DeepSeek 客户端
        self.client = OpenAI(
            api_key=deepseek_api_key, 
            base_url="https://api.deepseek.com"
        )
        # 初始化 Whisper (CPU int8 模式)
        self.whisper = WhisperModel("medium", device="cpu", compute_type="int8")
        self.music_dir = "/Users/randy/Downloads/temp"  # 你可以修改为任何你喜欢的目录
        if not os.path.exists(self.music_dir): os.makedirs(self.music_dir)

    def download_full_audio(self,query):
        print(f"🚀 正在全网搜索并下载整曲: {query}...")
        
        # 定义保存路径
        output_filename = os.path.join(self.music_dir, "full_audio")
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320', # 提升至最高音质，方便后续 AI 识别
            }],
            # --- 关键修改：去掉了时长限制参数 ---
            'outtmpl': f'{output_filename}.%(ext)s',
            'default_search': 'ytsearch1:', 
            'quiet': False, # 开启日志，下载整曲时你可以看到进度条
            'no_warnings': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"{query} Official Audio"])
            
        # yt-dlp 转换后会自动加上 .mp3 后缀
        return f"{output_filename}.mp3"
    
    def extract_lyrics(self, audio_path):
        print("AI 正在“听写”全曲歌词 (ASR)...")
        # 既然是整曲，large-v3 模型可能会跑 30-60 秒，取决于你的 GPU
        model = self.whisper
        
        # transcribe 会返回全曲内容
        segments, info = model.transcribe(audio_path, beam_size=5)
        
        print(f"检测到语种: {info.language} (置信度: {info.language_probability:.2f})")
        
        full_text = ""
        for segment in segments:
            # 加上时间戳，方便你以后对轴
            # print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
            full_text += f"{segment.text} "
            
        print(f"\n--- 听写完成 (全文摘要) ---")
        print(full_text[:500] + "...") 
        return full_text
    
    def analyze_audio(self,audio_path, lyrics_text=None):
        print("正在进行轻量化音频特征审计 (Librosa)...")
        
        # 优化 1：指定采样率 sr=22050 (足够分析节奏)，并仅加载前 180 秒以节省 CPU 内存
        # 如果歌曲短于 180s，它会自动加载全长
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
            
            print(f"--- 物理审计报告 ---")
            print(f"节奏 (BPM): {tempo:.2f}")
            print(f"能量感 (Energy): {energy:.4f}")
            print(f"时长 (Duration): {duration_minutes:.2f} 分钟")
            if word_density:
                print(f"词密度 (WPM): {word_density:.2f} 词/分钟")
            
            return {
                "tempo": float(tempo),
                "energy": float(energy),
                "duration_minutes": float(duration_minutes),
                "word_density": float(word_density) if word_density else None,
                "is_calm": energy < 0.15, # 根据你的黄金歌单，叙事类通常较安静
                "is_lyrical": 80 < tempo < 100 # J. Cole 风格常在的 BPM 区间
            }
            
        except Exception as e:
            print(f"音频审计失败: {e}")
            return None

    def _ai_audit(self, lyrics):
        prompt = f"""你是一位资深的 Hip-Hop 乐评人，审美标准深度对齐 J. Cole, Kendrick Lamar 和 Dave。
        你的任务是审计下方由 Whisper 识别出的原始歌词。
        审计维度（打分 0-10）：
        1. 叙事深度 (Storytelling)：是否在讲述一个连贯、深刻的人生故事或社会现象？
        2. 词作技巧 (Lyricism)：是否有复杂的押韵、隐喻或双关？
        3. 情感共鸣 (Vibe)：是否传递出沉思、奋斗、忧郁或清醒的情绪？
        4. 负面剔除 (Red Flags)：如果充满无意义的重复、炫富、纯粹的暴力，请大幅扣分。
        歌词内容：{lyrics[:3000]}
        请直接输出 JSON 格式：
        请输出 JSON 格式：
        {{
        "score": 0-100,
        "reason": "简短的中文评价",
        "key_lyrics": ["提取2-3句最灵魂的英文原文"],
        "decision": "Pass/Reject"
        }}"""
        response = self.client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "You are a professional music critic."},
                      {"role": "user", "content": prompt}],
            response_format={'type': 'json_object'}
        )
        return json.loads(response.choices[0].message.content)

    def run_full_audit(self, artist, track):
        print(f"--- 正在审计: {artist} - {track} ---")
        audio_path = self.download_full_audio(f"{artist} - {track}")
        
        full_lyrics = self.extract_lyrics(audio_path)
        # 1. 物理审计
        result= self.analyze_audio(audio_path, full_lyrics)
        
        # 4. DeepSeek 语义审计
        ai_report = self._ai_audit(full_lyrics)
        
        # 汇总报告
        report = {
            "track": f"{artist} - {track}",
            "metrics": {"bpm": result["tempo"], "energy": result["energy"], "wpm": result["word_density"]},
            "ai_audit": ai_report
        }
        
        # 清理文件
        if ai_report['decision'] == 'Reject':
            os.remove(audio_path)
            print("❌ 风格不符，已丢弃音源。")
        else:
            print(f"✅ 匹配成功！得分: {ai_report['score']}")
            
        return report

# --- 使用示例 ---
auditor = MusicAuditor(deepseek_api_key="sk-9fdc22a0cd39441aae1b3badfec3c120")

result = auditor.run_full_audit("Dave", "my 27th birthday")
print(json.dumps(result, indent=2, ensure_ascii=False))