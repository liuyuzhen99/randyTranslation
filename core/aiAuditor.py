import sqlite3
import os
import traceback
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

    def ai_audit_with_context(self, video_id, db_path,task_queue,vector_results):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT title, lyrics FROM videos WHERE video_id=?", (video_id,))
            video_info = cur.fetchall()
            title, lyrics = video_info[0] if video_info else ("Unknown Title", "")
            logger.info(f"✅ 成功从数据库读取歌曲:{title} video_id:{video_id} ")
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库video信息: {db_err}")
            logger.error(traceback.format_exc())
            return {"score": 0, "decision": "Reject", "reason": "数据库读取失败"}
        if not lyrics or len(lyrics) < 10:
            logger.warning("⚠️ 歌词文本过短或为空，跳过 AI 审计。")
            return {"score": 0, "decision": "Reject", "reason": "歌词内容不足以审计"}
        logger.info("🧠 正在请求 DeepSeek 进行歌词深度审计...")
        """
        结合向量库上下文进行审计
        vector_results: smart_search_by_current_video 返回的相似歌曲信息
        """
        # 提取历史参考资料作为 Few-Shot 案例
        reference_str = ""
        if vector_results and vector_results['ids'][0]:
            reference_str = "\n【参考库中已通过的高质量案例】:\n"
            for i in range(len(vector_results['ids'][0])):
                ref_title = vector_results['metadatas'][0][i].get('title', 'Unknown')
                ref_lyrics = vector_results['documents'][0][i][:300] # 取前300字
                reference_str += f"- 歌曲《{ref_title}》: {ref_lyrics}...\n"
        prompt = f"""你是一位资深的 Hip-Hop 乐评人，审美标准深度对齐 J. Cole, Kendrick Lamar 和 Dave。
        你的任务是审计下方由 Whisper 识别出的原始歌词。
        审计维度（打分 0-10）：
        1. 叙事深度 (Storytelling)：是否在讲述一个连贯、深刻的人生故事或社会现象？
        2. 词作技巧 (Lyricism)：是否有复杂的押韵、隐喻或双关？
        3. 情感共鸣 (Vibe)：是否传递出沉思、奋斗、忧郁或清醒的情绪？
        4. 负面剔除 (Red Flags)：如果充满无意义的重复、炫富、纯粹的暴力，请大幅扣分。
        要求：基于我库中已有的高审美标准（参考下方案例），审计这首新歌。
        {reference_str}
        待审计歌词：：{lyrics[:3000]}
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
            task_queue.put(("UPDATE_VIDEO_AUDIT", {
                'video_id': video_id,
                'score': score,
                'decision': decision
            }))
            logger.info("✅ 已将更新视频审计结果任务推送到数据库队列。")
            return audit_json
        except json.JSONDecodeError as je:
            logger.error(f"❌ AI 返回格式错误 (无法解析 JSON): {je}")
            task_queue.put(("UPDATE_VIDEO_AUDIT", {
                'video_id': video_id,
                'score': 0,
                'decision': "Failed"
            }))
        except Exception as e:
            logger.error(f"🚨 DeepSeek 接口调用失败: {e}")
            logger.error(traceback.format_exc())
            task_queue.put(("UPDATE_VIDEO_AUDIT", {
                'video_id': video_id,
                'score': 0,
                'decision': "Failed"
            }))
            
        return {"score": 0, "decision": "Error", "reason": "AI 审计接口异常"}