import json
import traceback
from openai import OpenAI
from utils.logger_manager import log_manager

logger = log_manager.get_task_logger("AI_REVIEWER")

class MusicReviewer:
    def __init__(self, base_url, api_key):
        logger.info("🛠️ 正在初始化 MusicReviewer (DeepSeek)...")
        try:
            self.client = OpenAI(
                api_key=api_key, 
                base_url=base_url
            ) 
            logger.info(f"✅ AI 客户端连接成功。BaseURL: {base_url}")
        except Exception as e:
            logger.error(f"🚨 AI 客户端初始化失败: {e}")
            raise

    def audit_transcription_segments(self, segments, max_len=80):
        """
        输入: transcribe 产生的 full_data (list of dict)
        输出: 修正后的 full_data (list of dict)
        """
        if not segments: return []
        
        # 1. 准备 Context 文本，带上序号
        raw_text_block = "\n".join([f"[{i}] {s['text']}" for i, s in enumerate(segments)])
        
        prompt = f"""你是一个极其挑剔的 Hip-hop 字幕校对员。
        任务：仅在“绝对必要”时合并 Whisper 的破碎片段。

        【禁止行为 - 严禁机械合并】：
        1. 严禁为了达到长度目标而将两个完整的句子或押韵行合并。
        2. 如果原行满足以下任一条件，必须保持原样（禁止合并）：
           - 已经是一个完整的意群（例如：I just bought a foreign car.）。
           - 长度已经超过 35 个字符。
           - 这是一个明显的押韵结束位。

        【准许行为 - 仅在以下情况合并】：
        1. 语义悬挂：前一行以连词（and, but, so）、介词（with, for, in）或助动词结尾，逻辑未完。
        2. 极度细碎：某行只有一个单词或短语（如：[0] Yeah, [1] I know），且合并后总长仍小于 {max_len}。

        【视觉目标】：
        保持歌词的“呼吸感”。我宁愿看到两行短而有力的歌词，也不愿看到一行冗长平庸的句子。

        输出 JSON 格式：
        {{"merges": [{{"start_idx": 0, "end_idx": 1, "new_text": "..."}}]}}。若现状合格，必须返回 {{"merges": []}}。

        待处理数据：
        {raw_text_block}
        """
        
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                response_format={'type': 'json_object'},
                temperature=0.2
            )
            instructions = json.loads(response.choices[0].message.content)
            merges = instructions.get("merges", [])
            
            if not merges:
                logger.info("✅ AI Reviewer: 未发现需要合并的断句。")
                return segments

            # 2. 执行内存合并逻辑
            new_segments = []
            merge_map = {m['start_idx']: m for m in merges}
            skip_until = -1

            for i, seg in enumerate(segments):
                if i <= skip_until:
                    continue
                
                if i in merge_map:
                    m = merge_map[i]
                    end_i = m['end_idx']
                    # --- 提取合并前的原始文本用于日志 ---
                    original_texts = [segments[j]['text'] for j in range(i, end_i + 1)]
                    combined_original = " / ".join(original_texts)

                    # 合并时间轴：起始行的 start，结束行的 end
                    new_segments.append({
                        'start': segments[i]['start'],
                        'end': segments[end_i]['end'],
                        'text': m['new_text']
                    })
                    # 打印详细对比日志
                    logger.info(f"🔗 【合并触发】索引 {i} -> {end_i}")
                    logger.info(f"   |  PREV: {combined_original}")
                    logger.info(f"   |  POST: {m['new_text']}")
                    skip_until = end_i
                    # logger.info(f"🔗 已合并索引 {i} 到 {end_i}: {m['new_text'][:30]}...")
                else:
                    new_segments.append(seg)
            
            return new_segments

        except Exception as e:
            logger.error(f"❌ AI 内存审计失败: {e}")
            return segments # 失败则返回原数据，保证流程不中断

    def audit_translation_map(self, srt_content):
        """
        输入: srt_content (包含序号、时间、EN、ZH 的完整字符串)
        输出: 修正后的 chinese_map {index: fixed_zh}
        """
        if not srt_content:
            return {}

        prompt = f"""你是一个顶级的 Hip-hop 译制总监，负责对整首歌的初翻稿进行“语境对齐”与“俚语润色”。
        
        【审计原则 - 宁缺毋滥】：
        你现在的审计行为会消耗昂贵的 Token。如果某一行翻译得很好，你将其加入 adjustments 列表是对系统资源的极大浪费。
        
        **判定标准**：
        - 只有当你能将该行优化得“更地道”、“更符合 Hip-hop 语境”或“修正了明显的行号错位”时，才准许加入。
        - 严禁出现 reason 为 "保留原译"、"无需修改"、"保持不变" 的条目。
        - 记住：返回 {{"adjustments": []}} 是你工作出色的最高体现，而非失职。

        【你的任务】：
        审视全文，确保整首歌的叙事逻辑（Context）一致，Slang 翻译地道，并解决跨行语义断裂。

        【核心审查维度】：
        1. **Slang & Wordplay 二次识别**：基于全文语境（如帮派、奋斗、成名），检查是否有隐藏的俚语被 Translator 直译了。
        2. **逻辑一致性（Flow）**：确保 [n] 行和 [n+1] 行的衔接自然。如果 [n] 行是问句，[n+1] 行的语气必须能接上。
        3. **代词一致性**：整首歌中对同一对象的称呼必须统一（例如：不能一会儿叫“兄弟”，一会儿叫“哥们”）。

        【绝对禁令 - 违反将导致程序崩溃】：
        - **禁止行间偏移**：严禁将 [[BLOCK_ID_n+1]] 的内容合并到 [[BLOCK_ID_n]]。每一行必须独立存在。
        - **禁止改变原意**：除非原译文有误或极度不通顺，否则保留 Translator 的 Few-shot 风格。

        【返回格式】：
        必须以 JSON 格式返回结果。
        仅返回需要修正的行，格式如下：
        {{"adjustments": [
            {{"index": 序号, "fixed_zh": "修正后的单行译文", "reason": "为什么要改（如：修正Slang、改善逻辑）"}}
          ]
        }}
        【JSON 数据结构严格要求】：
        adjustments 必须是一个包含对象的列表。每个对象必须且只能包含三个 key:
        - "index": (整数) 对应的行号锚点。
        - "fixed_zh": (字符串) 修正后的完整中文译文。
        - "reason": (字符串) 简短的修正理由。

        严禁返回类似 [1, 2, 3] 这种只有数字的列表。

        【零修正特殊处理】：
        如果经过全文审计，你认为初翻稿已经完美对齐语境、逻辑一致且 Slang 表达地道，无需任何修改，则必须返回一个空的 adjustments 列表。
        
        严禁为了凑数而进行无意义的微调。若无修正意见，返回格式必须严格为：
        {{"adjustments": []}}

        【审计反馈阈值】：
        只有当原译文存在“语义错误”、“逻辑断裂”、“俚语直译”或“代词冲突”时才进行修正。保持翻译的稳定性。
        
        待审阅全文对照：
        {srt_content}
        """

        try:
            # 保持上下文干净，使用局部消息
            messages = [
                {"role": "system", "content": "You are a senior lyricist and editor specializing in Rap culture."},
                {"role": "user", "content": prompt}
            ]

            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                response_format={'type': 'json_object'},
                temperature=0.2
            )

            result = json.loads(response.choices[0].message.content)
            adjustments = result.get("adjustments", [])

            # 转换为 {index: fixed_zh} 格式方便主函数回填
            # adjustment_map = {int(adj['index']): adj['fixed_zh'] for adj in adjustments}
            
            # 记录详细日志
            if adjustments:
                logger.info(f"🛡️ [Reviewer] 全文审计完成，共建议修正 {len(adjustments)} 处。")
                # logger.info(f"修正详情: {json.dumps(adjustments, ensure_ascii=False, indent=2)}")
            else:
                logger.info(f"✅ [Reviewer] 全文逻辑校验通过，无需变动。")

            return adjustments

        except Exception as e:
            logger.error(f"🚨 [Reviewer] 全文审计发生异常: {e}")
            return {}