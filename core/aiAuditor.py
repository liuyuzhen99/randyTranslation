import librosa
import numpy as np
import json
from openai import OpenAI  # DeepSeek 兼容 OpenAI 格式
import traceback
from utils.logger_manager import log_manager

# 初始化专门针对审计任务的 Logger
logger = log_manager.get_task_logger("MUSIC_AUDITOR")

class MusicAuditor:
    def __init__(self, base_url, api_key):
        logger.info("🛠️ 正在初始化 MusicAuditor (Librosa + DeepSeek)...")
        try:
            # 初始化 DeepSeek 客户端
            self.client = OpenAI(
                api_key=api_key, 
                base_url=base_url
            )
            logger.info(f"✅ AI 客户端连接成功。BaseURL: {base_url}")
        except Exception as e:
            logger.error(f"🚨 AI 客户端初始化失败: {e}")
            raise
    
    def analyze_audio(self,audio_path, lyrics_text):
        logger.info(f"📊 开始物理审计: {audio_path}")
        
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
            # 逻辑判定
            is_calm = energy < 0.15
            is_lyrical = 80 < tempo < 100
            
            audit_results = {
                "tempo": round(tempo, 2),
                "energy": round(energy, 4),
                "duration_minutes": round(duration_minutes, 2),
                "word_density": round(word_density, 2),
                "is_calm": is_calm,
                "is_lyrical": is_lyrical
            }
            logger.info(f"📈 审计结果: BPM={tempo:.1f}, Energy={energy:.4f}, WPM={word_density:.1f}")
            
            return audit_results
            
        except FileNotFoundError:
            logger.error(f"❌ 审计失败：找不到音频文件 {audio_path}")
        except Exception as e:
            logger.error(f"🚨 Librosa 物理审计发生未知错误: {e}")
            logger.error(traceback.format_exc())
        return None

    def ai_audit(self, lyrics):
        if not lyrics or len(lyrics) < 10:
            logger.warning("⚠️ 歌词文本过短或为空，跳过 AI 审计。")
            return {"score": 0, "decision": "Reject", "reason": "歌词内容不足以审计"}
        logger.info("🧠 正在请求 DeepSeek 进行歌词深度审计...")
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
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": "You are a professional music critic."},
                        {"role": "user", "content": prompt}],
                response_format={'type': 'json_object'}
            )
            audit_json = json.loads(response.choices[0].message.content)
            score = audit_json.get("score", 0)
            decision = audit_json.get("decision", "Reject")
            reason = audit_json.get('reason', None)
            
            logger.info(f"🤖 AI 审计完成：得分={score},理由={reason},决策={decision}")
            return audit_json
        except json.JSONDecodeError as je:
            logger.error(f"❌ AI 返回格式错误 (无法解析 JSON): {je}")
        except Exception as e:
            logger.error(f"🚨 DeepSeek 接口调用失败: {e}")
            logger.error(traceback.format_exc())
            
        return {"score": 0, "decision": "Error", "reason": "AI 审计接口异常"}