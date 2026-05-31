# from llama_cpp import Llama
import re
import os
import sqlite3
import traceback
from pathlib import Path
from openai import OpenAI
import json
from tenacity import retry, stop_after_attempt, wait_exponential
from data.translatorVectorDatabase import TranslationVectorManager
from core.aiReviewer import MusicReviewer
# 导入你的日志类实例
from utils.logger_manager import log_manager

_PROMPT_TEMPLATE = (Path(__file__).parent.parent / "prompts" / "translation_v1.txt").read_text(encoding="utf-8")

# 初始化专门针对翻译任务的 Logger
logger = log_manager.get_task_logger("TRANSLATOR")

# --- 辅助函数：时间格式化 ---
def format_timestamp(seconds: float):
    millis = int((seconds - int(seconds)) * 1000)
    td_sec = int(seconds)
    td_min, td_sec = divmod(td_sec, 60)
    td_hour, td_min = divmod(td_min, 60)
    return f"{td_hour:02}:{td_min:02}:{td_sec:02},{millis:03}"

class Translator:
    def __init__(self,api_key, base_url):
        logger.info("🏗️ 正在初始化翻译模型...")
        # 加载 Qwen 微调模型 (llama-cpp-python)
        try:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
            logger.info("🚀 已初始化长文本翻译引擎 (深度锚点模式)")
        except Exception as e:
            logger.error(f"🚨 翻译模型初始化失败: {e}")
            logger.error(traceback.format_exc())
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _call_deepseek(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a lyric synchronization expert."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            stream=False,
            timeout=30,
        )
        return response.choices[0].message.content
        # try:
        #     self.llm = Llama.from_pretrained(
        #         repo_id="Randyliu99/qwen2.5-7b-jcole-gguf",
        #         filename="Qwen2.5-7B-Instruct.Q4_K_M.gguf",
        #         n_ctx=2048,  # 设置为 2048 或更高，解决 1040 报错
        #         n_gpu_layers=-1 # 如果有显卡/金属加速，记得开启
        #     )
        #     logger.info("✅ 翻译模型加载完成，Metal 加速已启用。")
        # except Exception as e:
        #     logger.error(f"🚨 模型初始化失败: {e}")
        #     logger.error(traceback.format_exc())
        #     raise
    
    def _prepare_anchored_lyrics(self, english_texts):
        """
        将歌词包装成带 ID 的结构化文本，增强模型对行号的记忆
        """
        anchored_block = ""
        for i, text in enumerate(english_texts, start=1):
            anchored_block += f"<L{i}>{text}</L{i}>\n"
        return anchored_block
    
    def generate_bilingual_srt(self, video_id, task_queue, db_name, vector_manager=TranslationVectorManager()):
        try:
            conn = sqlite3.connect(db_name)
            conn.row_factory = sqlite3.Row # 使结果可以通过列名访问
            cur = conn.cursor()
            cur.execute("""
                SELECT v.title, s.line_index, s.start_time, s.end_time, s.en_text
                FROM subtitles s
                JOIN videos v ON s.video_id = v.video_id
                WHERE s.video_id = ?
                ORDER BY s.line_index ASC
            """, (video_id,))
            rows = cur.fetchall()
            conn.close()

            if not rows:
                logger.warning(f"⚠️ 数据库中未找到 video_id 为 {video_id} 的歌词数据。")
                return None
            
            # 转换为内部格式
            title = rows[0]['title']
            output_file = f"/Users/randy/Downloads/temp/{title}_{video_id}_final.srt"
            full_data = []
            english_texts = []
            for row in rows:
                full_data.append({
                    'start': row['start_time'],
                    'end': row['end_time'],
                    'text': row['en_text']
                })
                english_texts.append(row['en_text'])

            logger.info(f"✍️ 从数据库载入成功: {video_id}, 共 {len(english_texts)} 行。")
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库歌词信息: {db_err}")
            logger.error(traceback.format_exc())
            return None
        """
        full_data: 包含 start, end, text 的列表
        english_texts: 纯英文文本列表
        """
        if not full_data or not english_texts:
            logger.warning("⚠️ 传入的歌词数据为空，取消翻译任务。")
            return None
        # --- 步骤 A: 构建 Prompt ---
        # 将歌词合并，并带上序号，方便模型对应
        logger.info(f"✍️ 开始翻译任务，共 {len(english_texts)} 行歌词。")
        references = vector_manager.query_song_level_style(video_id, db_name,  n_results=3)
        logger.info(f"🔍 从向量库检索到 {len(references)} 个风格参考范例。")
        anchored_block = self._prepare_anchored_lyrics(english_texts)
        dynamic_few_shot = ""
        if references:
            dynamic_few_shot = "【风格参考范例（请模仿其翻译质感与遣词风格）】:\n"
            # for i, ref in enumerate(references):
            #     dynamic_few_shot += f"范例 {i+1} (来自 {ref['artist']}):\n原文: {ref['en']}\n译文: {ref['zh']}\n---\n"
            for i, ref in enumerate(references):
                dynamic_few_shot += f"范例 {i+1} (来自 {ref['artist']}):\n"
                
                # 假设 ref['en'] 和 ref['zh'] 是多行文本，我们将其按行拆分并打上标签
                en_lines = [line.strip() for line in ref['en'].split('\n') if line.strip()]
                zh_lines = [line.strip() for line in ref['zh'].split('\n') if line.strip()]
                
                # 确保中英行数一致，防止参考范例本身就错乱
                min_lines = min(len(en_lines), len(zh_lines))
                
                # 构建模仿兜底逻辑的 Input/Output 格式
                example_block = ""
                for idx in range(min_lines):
                    # 这里可以使用 L_ref 和 R_ref 避免与当前任务的行号混淆
                    # 或者直接模仿兜底使用 L1, R1...
                    example_block += f"Input: <L{idx+1}>{en_lines[idx]}</L{idx+1}>\n"
                    example_block += f"Output: <R{idx+1}>{zh_lines[idx]}</R{idx+1}>\n"
                
                dynamic_few_shot += f"{example_block}---\n"
        else:
            # 兜底逻辑
            dynamic_few_shot = """
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
        try:
            # lyrics_block = "\n".join([f"{i+1}: {text}" for i, text in enumerate(english_texts)])
            # estimated_tokens = len(lyrics_block.split()) * 1.5
            # if estimated_tokens > 1800:
            #     logger.warning(f"📏 歌词量较大 (约 {estimated_tokens:.0f} tokens)，可能接近 n_ctx 限制。")
            prompt = _PROMPT_TEMPLATE.format(
                dynamic_few_shot=dynamic_few_shot,
                anchored_block=anchored_block,
            )

            # --- 步骤 B: 调用本地模型翻译 ---
            logger.info("🧠 正在执行模型翻译...")
            raw_content = self._call_deepseek(prompt)
            chinese_map = {}
            patterns = re.findall(r'<R(\d+)>(.*?)</R\1>', raw_content, re.DOTALL)
            for idx_str, text in patterns:
                chinese_map[int(idx_str)] = text.strip()
            logger.info(f"🔍 正在校验对齐情况... 收到译文: {len(chinese_map)} 行")
            
            # --- 步骤 B.5: 全文 Review 环节 ---
            if chinese_map:
                reviewer = MusicReviewer(api_key=os.getenv("DEEPSEEK_API_KEY"),base_url=os.getenv("DEEPSEEK_BASE_URL"))
                logger.info("🎬 正在构造全文语境以供 Reviewer 审阅...")
                
                # 1. 构造包含时间轴和对照的文本流
                virtual_srt_blocks = []
                for i, item in enumerate(full_data, start=1):
                    cn_text = chinese_map.get(i, "")
                    # 重点：加入原文，但明确标注这是不可移动的物理行
                    block = f"[[BLOCK_ID_{i}]]\nSOURCE_EN: {item['text']}\nCURRENT_ZH: {cn_text}"
                    virtual_srt_blocks.append(block)
                full_srt_text = "\n\n".join(virtual_srt_blocks)
                
                # 2. 调用 Reviewer 获得修正建议
                adjustments = reviewer.audit_translation_map(full_srt_text)
                logger.info(f"🔍 Reviewer 建议修正 {len(adjustments)} 处。正在应用修正...")
                # logger.info(f"修正详情: {json.dumps(adjustments, ensure_ascii=False, indent=2)}")
                effective_changes = 0
                if not isinstance(adjustments, list):
                    logger.error(f"🚨 [Reviewer] 返回的 adjustments 格式异常: {type(adjustments)}")
                    return None
                for adj in adjustments:
                    try:
                        # --- 核心修复：类型检查 ---
                        if not isinstance(adj, dict):
                            # 如果 AI 返回的是 [1, 2, 3] 这种格式，adj 就是 int
                            logger.warning(f"⚠️ [跳过] 修正项不是字典对象: {adj} (类型: {type(adj)})")
                            continue

                        # 确保必要的 key 都在
                        if 'index' not in adj or 'fixed_zh' not in adj:
                            continue
                        idx = int(adj['index'])
                        new_zh = adj['fixed_zh'].strip().replace('\n', ' ') # 强制禁止换行符
                        old_zh = chinese_map.get(idx, "").strip()

                        # 1. 物理位置校验
                        if idx not in chinese_map:
                            logger.warning(f"⚠️ [跳过] Reviewer 返回了不存在的索引: {idx}")
                            continue

                        # 2. 实质性变化校验
                        if old_zh == new_zh:
                            continue

                        # 3. 长度溢出校验（防止 AI 把两句并一句导致变长）
                        if len(new_zh) > len(old_zh) + 15 and len(new_zh) > 30:
                            logger.warning(f"⚠️ [拦截] 行 {idx} 疑似被 AI 合并了后续内容，长度异常。")
                            continue

                        # 执行回填
                        logger.info(f"🔧 [Review修正] 行 {idx} | 理由: {adj.get('reason', '语义优化')}")
                        logger.info(f"   OLD: {old_zh}")
                        logger.info(f"   NEW: {new_zh}")
                        
                        chinese_map[idx] = new_zh
                        effective_changes += 1
                    except Exception as e:
                        logger.error(f"❌ 解析 Review 修正条目失败: {e}")

                logger.info(f"✅ 审计应用完毕，实际有效修正: {effective_changes} 处。")

            # --- 步骤 C: 组合生成 SRT ---
            subtitles_to_db = []
            with open(output_file, "w", encoding="utf-8") as f:
                for i, item in enumerate(full_data, start=1):
                    # 如果某行缺失，使用原文作为占位，防止 SRT 崩溃
                    cn_text = chinese_map.get(i, "")
                    if not cn_text:
                        logger.warning(f"⚠️ 第 {i} 行翻译缺失，已留空。")
                    start_str = format_timestamp(item['start'])
                    end_str = format_timestamp(item['end'])
                    
                    f.write(f"{i}\n")
                    f.write(f"{start_str} --> {end_str}\n")
                    f.write(f"{item['text']}\n")  # 英文原文
                    f.write(f"{cn_text}\n\n")    # 中文译文

                    # 准备批量回填 SQLite 的数据
                    subtitles_to_db.append({
                        'video_id': video_id,
                        'line_index': i - 1,
                        'zh_text': cn_text
                    })
            # --- 步骤 D: 批量回填 SQLite ---
            task_queue.put(("UPDATE_CH_SUBTITLES", {
                'srt_path': output_file,
                'subtitles_to_db': subtitles_to_db
            }))
            logger.info(f"✅ SRT 文件生成成功: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"🚨 翻译流程发生异常: {e}")
            logger.error(traceback.format_exc())
            return None