# from llama_cpp import Llama
import re
import traceback
from openai import OpenAI
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
        anchored_block = self._prepare_anchored_lyrics(english_texts)
        try:
            # lyrics_block = "\n".join([f"{i+1}: {text}" for i, text in enumerate(english_texts)])
            # estimated_tokens = len(lyrics_block.split()) * 1.5
            # if estimated_tokens > 1800:
            #     logger.warning(f"📏 歌词量较大 (约 {estimated_tokens:.0f} tokens)，可能接近 n_ctx 限制。")
            few_shot = """
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
            prompt = f"""你是一个专业的 Hip-hop 中文翻译官，能够准确理解并翻译嘻哈、R&B音乐，准确且地道不突兀，十分吸引听众。请将以下歌词翻译成中文。
            要求：
            - 逐行翻译下方 XML 标签内的内容。
            - 必须以 <R{{i}}>中文翻译</R{{i}}> 的格式返回。
            - 严禁合并行
            - 保持行数和序号一一对应。
            - 翻译要地道，保留歌词原本的俚语和韵味。
            - 只返回翻译后的中文，不要包含任何解释。
            - 语义通顺，能从全文的角度理解上下文含义。
            - 根据歌词的内容设定语境。
            - 不含脏话，敏感词汇会进行隐晦处理。

            【参考示例】:
            {few_shot}

            歌词列表：
            {anchored_block}

            直接返回结果，不要任何开场白。
            """

            # --- 步骤 B: 调用本地模型翻译 ---
            logger.info("🧠 正在执行模型翻译...")
            # response = self.llm.create_chat_completion(
            #     messages=[{"role": "user", "content": prompt}],
            #     max_tokens=2048, # 歌词长的话需要调大
            #     temperature=0.7
            # )
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a lyric synchronization expert."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3, # 降低随机性，增强对齐严谨度
                stream=False
            )
            raw_content = response.choices[0].message.content
            chinese_map = {}
            patterns = re.findall(r'<R(\d+)>(.*?)</R\1>', raw_content, re.DOTALL)
            for idx_str, text in patterns:
                chinese_map[int(idx_str)] = text.strip()
            logger.info(f"🔍 正在校验对齐情况... 收到译文: {len(chinese_map)} 行")
            # translated_content = response['choices'][0]['message']['content']
            
            # 解析模型返回的中文（假设模型按行返回）
            # 简单的清理逻辑：去掉序号，只留文本
            # chinese_lines = []
            # for line in translated_content.strip().split('\n'):
            #     # 去掉类似 "1: " 或 "1. " 的前缀
            #     clean_line = re.sub(r'^\d+[:.、\s]+', '', line)
            #     chinese_lines.append(clean_line)

            # --- 步骤 C: 组合生成 SRT ---
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
            logger.info(f"✅ SRT 文件生成成功: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"🚨 翻译流程发生异常: {e}")
            logger.error(traceback.format_exc())
            return None