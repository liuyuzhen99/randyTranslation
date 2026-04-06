import json
from utils.logger_manager import log_manager

logger = log_manager.get_task_logger("AI_REVIEWER")

class MusicReviewer:
    def __init__(self, client):
        self.client = client # 传入已经初始化好的 OpenAI/DeepSeek client

    def audit_transcription_segments(self, segments):
        """
        输入: transcribe 产生的 full_data (list of dict)
        输出: 修正后的 full_data (list of dict)
        """
        if not segments: return []
        
        # 1. 准备 Context 文本，带上序号
        raw_text_block = "\n".join([f"[{i}] {s['text']}" for i, s in enumerate(segments)])
        
        prompt = f"""你是一个专业的 Hip-hop 歌词校对专家。
        任务：识别下方由 Whisper 识别出的歌词中，由于断句错误导致语义破碎的行，并将它们合并。
        要求：
        1. 仅当某几行在语法或逻辑上属于一句话时进行合并。
        2. 严禁修改或增减单词，只能合并。
        3. 返回 JSON 格式，包含合并后的索引范围和完整文本。
        示例输出：{{"merges": [{{"start_idx": 3, "end_idx": 4, "new_text": "complete sentence"}}]}}
        如果没有需要合并的，返回 {{"merges": []}}。
        
        待处理歌词：
        {raw_text_block}
        """
        
        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                response_format={'type': 'json_object'}
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
                    # 合并时间轴：起始行的 start，结束行的 end
                    new_segments.append({
                        'start': segments[i]['start'],
                        'end': segments[end_i]['end'],
                        'text': m['new_text']
                    })
                    skip_until = end_i
                    logger.info(f"🔗 已合并索引 {i} 到 {end_i}: {m['new_text'][:30]}...")
                else:
                    new_segments.append(seg)
            
            return new_segments

        except Exception as e:
            logger.error(f"❌ AI 内存审计失败: {e}")
            return segments # 失败则返回原数据，保证流程不中断

    def audit_translation_map(self, english_list, chinese_map):
        """
        用于在写入数据库前，对翻译出的字典进行最后的语义润色
        """
        # 这里可以实现你提到的“审查翻译准确度”逻辑
        # 输入 chinese_map {1: "译文1", 2: "译文2"...}
        # 输出 润色后的 chinese_map
        pass