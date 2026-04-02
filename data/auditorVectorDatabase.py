import chromadb
import sqlite3
from chromadb.utils import embedding_functions
import os
import traceback
from utils.logger_manager import log_manager

# 初始化专门针对向量数据库任务的 Logger
logger = log_manager.get_task_logger("AUDITOR_VECTOR_DB")

class MusicVectorCommander:
    def __init__(self, db_path="./data/chroma_db"):
        # 自动处理路径
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 1. 初始化持久化客户端
        self.client = chromadb.PersistentClient(path=db_path)
        
        # 2. 定义 Embedding 模型 (商业化时可轻松换成 OpenAI/HuggingFace)
        self.emb_fn = embedding_functions.DefaultEmbeddingFunction()
        
        # 3. 获取/创建集合
        self.collection = self.client.get_or_create_collection(
            name="user_taste_v1",
            embedding_function=self.emb_fn
        )

    def sync_to_vector_db(self, db_path, video_id):
        """
        核心：将多模态数据对齐并存入
        """
        try:
            # 1. 连库读取已激活且有 ID 的艺人
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            # 只查活跃且有 YouTube ID 的艺人
            query = """
                SELECT v.title, v.bpm, v.energy, v.word_density, v.lyrics, a.name
                FROM videos v
                LEFT JOIN artists a ON v.spotify_id = a.spotify_id
                WHERE v.video_id = ?
            """
            cur.execute(query, (video_id,))
            row = cur.fetchone()
            conn.close()
            if not row:
                logger.warning(f"⚠️ 数据库中未找到 video_id: {video_id}，同步取消。")
                return

            # 解构数据
            title, bpm, energy, word_density, lyrics, artist_name, = row
            
            # 数据清洗：确保没有空值
            artist_name = artist_name if artist_name else "Unknown Artist"
            lyrics = lyrics if lyrics else ""

            logger.info(f"💾 准备同步歌曲: {title} (ID: {video_id})")
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库video信息: {db_err}")
            logger.error(traceback.format_exc())
            return
        try:
            # 归一化处理
            # 2. 构建 Metadata (必须与 smart_search 的 Key 完全一致)
            # 注意：ChromaDB 的 Metadata 不支持存储 List 类型，只能存 str, int, float, bool
            metadata = {
                "video_id": video_id,
                "title": title,
                "artist": artist_name,
                "bpm": float(bpm),
                "energy": round(float(energy), 2),
                "word_density": round(float(word_density), 1),
                # "genres": genres_str,  # 存储完整字符串用于 $contains 检索
            }

            # 存入向量库
            self.collection.add(
                documents=[lyrics],      # 语义搜索的基准
                metadatas=[metadata],    # 过滤的基准
                ids=[video_id]           # 唯一标识
            )
            # print(f"✅ 已对齐并存入向量库: {video_id}")
            logger.info(f"✨ 向量库同步成功: [{artist_name}] - {title}")
        except Exception as vdb_err:
            logger.error(f"🚨 ChromaDB 写入失败: {vdb_err}")
            logger.error(traceback.format_exc())


    def smart_search_by_current_video(self, db_path, video_id, n_results=3):
        """
        基于当前已分析出的数据，去向量库检索最匹配的历史案例
        注意：此时这首歌可能还没正式 insert 进向量库，或者我们想搜除了它以外的歌
        """
        try:
            # 1. 连库读取已激活且有 ID 的艺人
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            # 只查活跃且有 YouTube ID 的艺人
            # 联合查询：获取视频特征、艺人名及流派
            query = """
                SELECT v.title, v.bpm, v.energy, v.word_density, v.lyrics, a.name
                FROM videos v
                LEFT JOIN artists a ON v.spotify_id = a.spotify_id
                WHERE v.video_id = ?
            """
            cur.execute(query, (video_id,))
            row = cur.fetchone()
            conn.close()
            if not row:
                logger.warning(f"⚠️ 数据库中未找到 video_id: {video_id}")
                return None
            title, bpm, energy, word_density, lyrics, artist_name,  = row
            # 处理流派：将字符串转为列表，方便后续处理（如果需要）
            
        except Exception as db_err:
            logger.error(f"🚨 无法读取数据库video信息: {db_err}")
            logger.error(traceback.format_exc())
            return None
        try:
            # 逻辑：库里存的 genres 字段如果包含当前艺人的任何一个流派，即视为匹配
            # 1. 定义多维物理过滤条件
            # 逻辑：BPM 差距 15% 以内，能量 0.2 以内，词密度 20% 以内
            where_filter = {
                "$and": [
                    {"artist": {"$eq": artist_name}},
                    {"bpm": {"$gte": bpm * 0.9}}, # 缩小到10%误差
                    {"bpm": {"$lte": bpm * 1.1}}, # 缩小到10%误差
                    {"energy": {"$gte": energy - 0.15}},
                    {"energy": {"$lte": energy + 0.15}},
                    {"word_density": {"$gte": word_density * 0.85}},
                    {"word_density": {"$lte": word_density * 1.15}}
                    # 只有当 current_genres 不为空时才加入流派过滤
                    #{"$or": genre_filters} if genre_filters else {}
                ]
            }
            # 清理空字典（如果 genre_filters 为空）

            # 2. 执行语义检索 (使用当前歌词作为 Query)
            # ChromaDB 会自动将 query_texts 转换为向量
            results = self.collection.query(
                query_texts=[lyrics],
                n_results=n_results,
                where=where_filter,
                # 排除当前正在处理的 video_id，避免搜到自己（如果已存在）
                where_document={"$not_contains": video_id} if video_id else None 
            )

            # 3. 🛡️ 退避逻辑 (Fallback): 如果物理条件太苛刻搜不到，则放宽限制
            if not results['ids'] or len(results['ids'][0]) == 0:
                logger.info(f"🔎 第一层未命中。正在尝试【全流派范围】下的物理特征检索...")
                fallback_filter = {
                    "$and": [
                        {"bpm": {"$gte": bpm * 0.85}},
                        {"bpm": {"$lte": bpm * 1.15}},
                        {"energy": {"$gte": energy - 0.2}},
                        {"energy": {"$lte": energy + 0.2}},
                        {"word_density": {"$gte": word_density * 0.8}},
                        {"word_density": {"$lte": word_density * 1.2}}
                    ]
                }
                results = self.collection.query(
                    query_texts=[lyrics],
                    n_results=n_results,
                    where=fallback_filter
                )
            
            # --- 4. 第三层：兜底检索 (仅语义 + 基础物理特征) ---
            if not results['ids'] or len(results['ids'][0]) == 0:
                logger.warning("⚠️ 第二层未找到。执行最终兜底：纯语义 + 宽泛物理特征...")
                where_level_3 = {
                    "$and": [
                        {"energy": {"$gte": energy - 0.3}},
                        {"energy": {"$lte": energy + 0.3}}
                    ]
                }
                results = self.collection.query(query_texts=[lyrics], n_results=n_results, where=where_level_3)

            logger.info(f"🔎 已为视频 {video_id} 检索到 {len(results['ids'][0])} 个相似风格案例")
            return results

        except Exception as e:
            logger.error(f"🚨 向量检索失败: {e}")
            return None