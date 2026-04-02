import traceback

import chromadb
from chromadb.utils import embedding_functions
import sqlite3
import pandas as pd
import os
from utils.logger_manager import log_manager
import re

logger = log_manager.get_task_logger("TRANSLATION_VECTOR_DB")


def parse_manual_srt_flexible(file_content):
    """
    自适应解析器：自动识别[中-英]或[英-中]格式的 SRT
    """
    # 匹配模式：序号 -> 时间轴 -> 文本行1 -> 文本行2
    pattern = re.compile(
        r'(\d+)\n'
        r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n'
        r'(.*?)\n'
        r'(.*)',
        re.DOTALL
    )
    
    def is_chinese(text):
        # 简单的中文字符正则判断
        return bool(re.search(r'[\u4e00-\u9fa5]', text))

    def srt_to_seconds(s):
        h, m, s_ms = s.split(':')
        s, ms = s_ms.split(',')
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

    blocks = file_content.strip().split('\n\n')
    results = []
    
    for i, block in enumerate(blocks):
        match = pattern.search(block)
        if match:
            _, start_str, end_str, line1, line2 = match.groups()
            
            # 自动判断哪行是中文，哪行是英文
            if is_chinese(line1):
                zh_text, en_text = line1, line2
            else:
                en_text, zh_text = line1, line2

            results.append({
                'line_index': i,
                'start_time': srt_to_seconds(start_str),
                'end_time': srt_to_seconds(end_str),
                'en': en_text.strip(),
                'zh': zh_text.strip()
            })
    return results

logger = log_manager.get_task_logger("TRANSLATION_VECTOR_DB")

class TranslationVectorManager:
    def __init__(self, db_path="./data/chroma_db"):
        # 自动处理路径
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 2. 初始化持久化客户端
        self.client = chromadb.PersistentClient(path=db_path)

        # 3. 定义 Embedding 模型 (商业化时可轻松换成 OpenAI/HuggingFace)
        self.emb_fn = embedding_functions.DefaultEmbeddingFunction()
        
        # 4. 获取或创建翻译记忆库集合
        # 默认使用 Chroma 內置的 embedding 功能 (all-MiniLM-L6-v2)
        self.collection = self.client.get_or_create_collection(
            name="translation_memory",
            embedding_function=self.emb_fn,
            metadata={"hnsw:space": "cosine"} # 使用余弦相似度，最适合文本对比
        )

    def sync_song_to_translation_db(self, video_id, db_path, artist, tags):
        """
        从 SQLite 提取人工复审通过的字幕，切片并同步到 ChromaDB
        """
        try:
            # 1. 从 SQLite 读取该视频所有已确认的行级对齐数据
            conn = sqlite3.connect(db_path)
            # 确保按 line_index 排序，维持歌词流向
            query = f"SELECT en_text, zh_text FROM subtitles WHERE video_id = ? ORDER BY line_index"
            df = pd.read_sql(query, conn, params=(video_id,))
            conn.close()

            if df.empty:
                print(f"⚠️ 视频 {video_id} 在 subtitles 表中无数据，跳过同步。")
                return

            en_lines = df['en_text'].tolist()
            zh_lines = df['zh_text'].tolist()

            # 2. 设定滑动窗口参数
            window_size = 5  # 每 5 行作为一个语义块 (Chunk)
            step = 3        # 步长为 3，意味着每块之间有 2 行重叠 (Overlapping)
            
            documents = []
            metadatas = []
            ids = []

            for i in range(0, len(en_lines), step):
                end = i + window_size
                # 提取英文块（用于生成向量）和对应的中文块（作为翻译参考）
                en_chunk = " ".join(en_lines[i:end]).strip()
                zh_chunk = "\n".join(zh_lines[i:end]).strip()

                if not en_chunk:
                    continue

                # 3. 构造唯一的 Chunk ID
                chunk_id = f"{video_id}_chunk_{i}"
                
                documents.append(en_chunk)
                metadatas.append({
                    "video_id": video_id,
                    "artist": artist,
                    # "tags": ",".join(tags) if isinstance(tags, list) else tags,
                    "chinese_vibe": zh_chunk,  # ✨ 核心：未来的翻译参考模版
                    "start_line": i
                })
                ids.append(chunk_id)

                if end >= len(en_lines):
                    break

            # 4. 批量写入 ChromaDB
            if documents:
                self.collection.upsert(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )
                logger.info(f"✅ 成功同步视频 {video_id} 的 {len(documents)} 个语义块到向量库。")

        except Exception as e:
            logger.error(f"❌ 同步到向量库失败: {e}")
    
    def import_manual_sample_to_vector_db(self, artist, file_path, tags=None):
        """
        直接将手动 SRT 样本导入 ChromaDB，跳过 SQLite
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 1. 使用我们之前的自适应解析器获取数据
            parsed_data = parse_manual_srt_flexible(content)
            
            if not parsed_data:
                logger.error(f"⚠️ 文件 {file_path} 解析出空数据，请检查格式。")
                return

            # 2. 提取纯文本列表用于切片
            en_lines = [item['en'] for item in parsed_data]
            zh_lines = [item['zh'] for item in parsed_data]
            
            # 3. 语义切片逻辑 (滑动窗口)
            window_size = 5
            step = 3
            
            documents = []
            metadatas = []
            ids = []
            
            # 生成一个基于文件名的唯一 ID 前缀，防止冲突
            file_prefix = os.path.basename(file_path).split('.')[0]

            for i in range(0, len(en_lines), step):
                end = i + window_size
                en_chunk = " ".join(en_lines[i:end]).strip()
                zh_chunk = "\n".join(zh_lines[i:end]).strip()

                if not en_chunk:
                    continue

                # 构造 ID
                chunk_id = f"manual_{file_prefix}_{i}"
                
                documents.append(en_chunk)
                metadatas.append({
                    "source": "manual_upload",
                    "artist": artist,
                    # "tags": ",".join(tags) if tags else "General",
                    "chinese_vibe": zh_chunk, # DeepSeek 翻译时的核心参考
                    "is_gold_standard": True  # 标记为高质量手动样本
                })
                ids.append(chunk_id)

                if end >= len(en_lines):
                    break

            # 4. 直接推送到向量库
            if documents:
                self.collection.upsert(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )
                logger.info(f"✅ 成功将手动样本 {file_prefix} 同步至 ChromaDB (共 {len(documents)} 个语义块)")

        except Exception as e:
            logger.error(f"❌ 手动导入向量库失败: {e}")

    def query_song_level_style(self, video_id, db_path, n_results=5):
        try:
            # 1. 从数据库中获取歌曲的完整歌词
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            query = "SELECT lyrics FROM videos WHERE video_id = ?"
            cur.execute(query, (video_id,))
            row = cur.fetchone()
            conn.close()
            if not row:
                logger.warning(f"⚠️ 数据库中未找到 video_id: {video_id} 的歌词信息。")
                return []
            full_lyrics_en = row[0]
            if not full_lyrics_en:
                logger.warning(f"⚠️ video_id: {video_id} 的歌词内容为空。")
                return []
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库歌词信息: {db_err}")
            logger.error(traceback.format_exc())
            return []
        """
        为整首歌寻找 5 个最具代表性的审美参考点
        """
        # 提取歌词中信息熵最高的部分（通常是第一段或副歌）进行检索
        # 或者直接对全文进行摘要检索
        search_query = full_lyrics_en[:3000] # 取前3000字作为特征锚点
        
        results = self.collection.query(
            query_texts=[search_query],
            n_results=n_results,
            include=['documents', 'metadatas', 'distances']
        )

        references = []
        for i in range(len(results['ids'][0])):
            if results['distances'][0][i] < 0.6: # 全篇匹配时放宽一点阈值
                meta = results['metadatas'][0][i]
                references.append({
                    "en": results['documents'][0][i],
                    "zh": meta.get('chinese_vibe'),
                    "artist": meta.get('artist')
                })
        return references