from llama_cpp import Llama
import re
import traceback
# 导入你的日志类实例
from utils.logger_manager import log_manager

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
    def __init__(self):
        logger.info("🏗️ 正在初始化翻译模型...")
        # 加载 Qwen 微调模型 (llama-cpp-python)
        try:
            self.llm = Llama.from_pretrained(
                repo_id="Randyliu99/qwen2.5-7b-jcole-gguf",
                filename="Qwen2.5-7B-Instruct.Q4_K_M.gguf",
                n_ctx=2048,  # 设置为 2048 或更高，解决 1040 报错
                n_gpu_layers=-1 # 如果有显卡/金属加速，记得开启
            )
            logger.info("✅ 翻译模型加载完成，Metal 加速已启用。")
        except Exception as e:
            logger.error(f"🚨 模型初始化失败: {e}")
            logger.error(traceback.format_exc())
            raise
    
    def generate_bilingual_srt(self,full_data, english_texts, output_file):
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
        try:
            lyrics_block = "\n".join([f"{i+1}: {text}" for i, text in enumerate(english_texts)])
            estimated_tokens = len(lyrics_block.split()) * 1.5
            if estimated_tokens > 1800:
                logger.warning(f"📏 歌词量较大 (约 {estimated_tokens:.0f} tokens)，可能接近 n_ctx 限制。")
            prompt = f"""你是一个专业的 Hip-hop 翻译官。请将以下歌词翻译成中文。
            要求：
            1. 保持行数和序号一一对应。
            2. 翻译要地道，保留 Rap 的俚语和韵味。
            3. 只返回翻译后的中文，不要包含任何解释。
            4. 语义通顺并能理解上下文含义。
            5. 根据歌词的内容设定语境。
            6. 不含脏话，敏感词汇会进行隐晦处理。

            歌词列表：
            {lyrics_block}
            """

            # --- 步骤 B: 调用本地模型翻译 ---
            logger.info("🧠 正在执行模型翻译...")
            response = self.llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048, # 歌词长的话需要调大
                temperature=0.7
            )
            
            translated_content = response['choices'][0]['message']['content']
            
            # 解析模型返回的中文（假设模型按行返回）
            # 简单的清理逻辑：去掉序号，只留文本
            chinese_lines = []
            for line in translated_content.strip().split('\n'):
                # 去掉类似 "1: " 或 "1. " 的前缀
                clean_line = re.sub(r'^\d+[:.、\s]+', '', line)
                chinese_lines.append(clean_line)

            # --- 步骤 C: 组合生成 SRT ---
            with open(output_file, "w", encoding="utf-8") as f:
                for i, (item, cn_text) in enumerate(zip(full_data, chinese_lines), start=1):
                    start_str = format_timestamp(item['start'])
                    end_str = format_timestamp(item['end'])
                    
                    f.write(f"{i}\n")
                    f.write(f"{start_str} --> {end_str}\n")
                    f.write(f"{item['text']}\n")  # 英文原文
                    f.write(f"{cn_text}\n\n")    # 中文译文
            logger.info(f"✅ SRT 文件生成成功: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"🚨 翻译流程发生异常: {e}")
            logger.error(traceback.format_exc())
            return None