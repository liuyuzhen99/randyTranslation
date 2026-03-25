import sqlite3
import threading
import queue
import time
import random
from datetime import datetime
from services.getSpotifyFollowingList import get_all_followed_artists
from services.getChannelIDfromFollowingList import fetch_youtube_channel_ids
from services.getLatestMVfromRss import job_rss_scanner
from core.aiAuditor import MusicAuditor
from core.aiTranslator import Translator
from core.audioTranscriber import SeparateTranscriber
from core.videoMaker import burn_video
from core.ytbAVDownloader import download_step_audio, download_step_video
import os
from dotenv import load_dotenv
import traceback
from utils.logger_manager import log_manager

logger = log_manager.get_task_logger("ORCHESTRATOR")

load_dotenv()  # 从 .env 文件加载环境变量

# from apscheduler.schedulers.background import BackgroundScheduler

# ==========================================
# 1. 数据库基础操作（放在主线程初始化）
# ==========================================

# def init_db(db_name):
#     logger.info(f"🏗️ 正在初始化数据库: {db_name}")
#     try:
#         conn = sqlite3.connect(db_name)
#         cursor = conn.cursor()
        
#         # 1. 创建 artists 表 (保存艺人基础信息)
#         cursor.execute('''
#             CREATE TABLE IF NOT EXISTS artists (
#                 spotify_id TEXT PRIMARY KEY,
#                 name TEXT,
#                 yt_channel_id TEXT,
#                 status TEXT DEFAULT 'active',
#                 is_manual INTEGER DEFAULT 0,
#                 last_sync_at DATETIME,
#                 last_yt_search_at DATETIME
#             )
#         ''')
#         # 建立索引优化查询性能
#         cursor.execute('CREATE INDEX IF NOT EXISTS idx_status_yt ON artists(status, yt_channel_id);')

#         # 2. 创建 videos 表 (保存扫描到的 MV 记录)
#         # 使用 video_id 作为主键，确保同一个视频不会被重复插入
#         cursor.execute('''
#             CREATE TABLE IF NOT EXISTS videos (
#                 video_id TEXT PRIMARY KEY,
#                 spotify_id TEXT,
#                 title TEXT,
#                 link TEXT,
#                 published_at DATETIME,
#                 processed_status TEXT DEFAULT 'new', -- 状态：new(新发现), processing(处理中), completed(已完成), skipped(跳过)
#                 FOREIGN KEY (spotify_id) REFERENCES artists (spotify_id)
#             )
#         ''')
#         # 为视频发布时间建立索引，方便查找“近两周”的数据
#         cursor.execute('CREATE INDEX IF NOT EXISTS idx_published_at ON videos(published_at);')
#         # 为处理状态建立索引，方便翻译 Agent 快速捞取新任务
#         cursor.execute('CREATE INDEX IF NOT EXISTS idx_proc_status ON videos(processed_status);')

#         conn.commit()
#         conn.close()
#         logger.info("✅ 数据库架构(Schema)初始化完成。")
#     except Exception as e:
#         logger.critical(f"🚨 数据库初始化致命错误: {e}")
#         logger.error(traceback.format_exc())
#         raise


class DatabaseManager:
    """
    中央调度管理器：整合数据库操作与核心生产流水线
    """
    def __init__(self, db_path="data/music_agent.db"):
        self.db_path = db_path
        self.task_queue = queue.Queue()
        self._init_db()
        
        # 初始化数据库消费者线程
        self.consumer = DatabaseConsumer(self.db_path, self.task_queue)
        self.consumer.start()

    def _init_db(self):
        logger.info(f"🏗️ 正在初始化数据库: {self.db_path}")
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # 艺人表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS artists (
                    spotify_id TEXT PRIMARY KEY,
                    name TEXT,
                    yt_channel_id TEXT,
                    status TEXT DEFAULT 'active',
                    last_sync_at DATETIME,
                    last_yt_search_at DATETIME
                )
            ''')
            # 视频流水线表：新增了更多状态字段
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    spotify_id TEXT,
                    title TEXT,
                    published_at DATETIME,
                    processed_status TEXT DEFAULT 'new', 
                    audit_score INTEGER,
                    local_video_path TEXT,
                    srt_path TEXT,
                    final_video_path TEXT,
                    error_msg TEXT,
                    FOREIGN KEY (spotify_id) REFERENCES artists (spotify_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_proc_status ON videos(processed_status);')
            conn.commit()
            conn.close()
        except Exception as e:
            logger.critical(f"🚨 数据库初始化失败: {e}")
            raise

    def add_task(self, action, data):
        """向调度中心提交任务"""
        self.task_queue.put((action, data))

# ==========================================
# 2. 数据库写入消费者 (唯一写者)
# ==========================================
class DatabaseConsumer(threading.Thread):
    def __init__(self, db_name, task_queue):
        super().__init__()
        self.db_name = db_name
        self.task_queue = task_queue
        self.daemon = True
        self.logger = log_manager.get_task_logger("PIPELINE_WORKER")

    def run(self):
        self.logger.info("🚀 生产流水线守护线程已就绪...")
        try:
            conn = sqlite3.connect(self.db_name, timeout=30)
            conn.execute('PRAGMA journal_mode=WAL;') # 必开，支持并发读
            cursor = conn.cursor()
        except Exception as e:
            self.logger.critical(f"❌ 数据库连接失败，写线程终止: {e}")
            return
        
        while True:
            task = self.task_queue.get()
            if task is None: 
                self.logger.info("👋 收到停止信号，正在关闭写线程...")
                break # 收到 None 则退出
            action, data = task
            self.logger.info(f"📥 接收到写操作请求: [{action}]")
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if action == "SYNC_SPOTIFY":
                    # print(f"条数确认: 准备写入 {len(data)} 条数据")
                    if not data: 
                        self.logger.warning("⚠️ SYNC_SPOTIFY 接收到空列表，跳过更新。")
                        continue
                    ids = [a['id'] for a in data]
                    # 1. 逻辑删除不在新列表中的艺人
                    cursor.execute(f"UPDATE artists SET status='unfollowed' WHERE spotify_id NOT IN ({','.join(['?']*len(ids))})", ids)
                    # 2. 增量更新/插入
                    insert_data = [(a['id'], a['name'], now) for a in data]
                    cursor.executemany('''
                        INSERT INTO artists (spotify_id, name, status, last_sync_at)
                        VALUES (?, ?, 'active', ?)
                        ON CONFLICT(spotify_id) DO UPDATE SET 
                            status='active', 
                            last_sync_at=excluded.last_sync_at,
                            name=excluded.name
                    ''', insert_data)
                    self.logger.info(f"✅ Spotify 同步完成，处理记录: {len(data)} 条")
                    # for a in data:
                    #     cursor.execute('''
                    #         INSERT INTO artists (spotify_id, name, status, last_sync_at)
                    #         VALUES (?, ?, 'active', ?)
                    #         ON CONFLICT(spotify_id) DO UPDATE SET 
                    #             status='active', 
                    #             last_sync_at=excluded.last_sync_at,
                    #             name=excluded.name
                    #     ''', (a['id'], a['name'], now))
                        
                elif action == "UPDATE_YT_ID":
                    cursor.execute('''
                        UPDATE artists SET 
                            yt_channel_id = ?, 
                            last_yt_search_at = ? 
                        WHERE spotify_id = ?
                    ''', (data['yt_id'], now, data['sid']))
                    if data['yt_id']:
                        self.logger.info(f"✅ YouTube ID 更新: {data['name']} -> {data['yt_id']}")
                    else:
                        self.logger.warning(f"⚠️ {data['name']} 的 YouTube ID 查询结果为空。")
                elif action == "NEW_VIDEO_FOUND":
                    # 核心：使用 INSERT OR IGNORE 配合 PRIMARY KEY(video_id) 实现物理去重
                    cursor.execute('''
                        INSERT OR IGNORE INTO videos (video_id, spotify_id, title, published_at, processed_status)
                        VALUES (?, ?, ?, ?, 'new')
                    ''', (data['video_id'], data['spotify_id'], data['title'], data['published_at']))
                    if cursor.rowcount > 0:
                        self.logger.info(f"🆕 发现新 MV 并存库: {data['title']}")
                                
                conn.commit()
            except Exception as e:
                self.logger.error(f"❌ 数据库写入操作 [{action}] 失败: {e}")
                self.logger.error(traceback.format_exc())
                conn.rollback()
            finally:
                self.task_queue.task_done()
        conn.close()

# ==========================================
# 3. 生产者逻辑
# ==========================================

# 任务 A: 每月同步 Spotify
def job_sync_spotify(q):
    logger.info("🚀 触发任务: Spotify 关注列表同步...")
    # 这里接入你的 Spotify API 函数
    try:
        followed_list = get_all_followed_artists()
        if followed_list:
            q.put(("SYNC_SPOTIFY", followed_list))
            logger.info(f"✅ 已抓取 {len(followed_list)} 位艺人并推送至写入队列。")
        else:
            logger.warning("⚠️ 未能从 Spotify 获取到任何艺人数据。")
    except Exception as e:
        logger.error(f"🚨 Spotify 同步作业失败: {e}")

# 任务 B: 每周填充 YouTube ID (受控并发)
def job_fill_youtube_ids(db_name, q, batch_size=20):
    logger.info(f"🔍 触发任务: 增量抓取 YouTube ID (BatchSize: {batch_size})...")
    targets = []
    try:
        # 读操作：直接连数据库查，不进队列（因为开启了 WAL）
        conn = sqlite3.connect(db_name)
        cur = conn.cursor()
        cur.execute('''
            SELECT spotify_id, name FROM artists 
            WHERE status='active' AND yt_channel_id IS NULL 
            ORDER BY last_yt_search_at ASC LIMIT ?
        ''', (batch_size,))
        targets = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"🚨 读取待补充 ID 的艺人列表失败: {e}")
        return
    if not targets:
        logger.info("☕ 没有需要补充 YouTube ID 的艺人，休息一下。")
        return
    
    for sid, name in targets:
        try:
            logger.info(f"👨‍💻 正在通过浏览器查询艺人: {name}")
            # 这里接入你的 DrissionPage 抓取函数
            yt_id = fetch_youtube_channel_ids([name]).get(name, None) # 返回可能是 None
            # yt_id = fetch_youtube_id(name) 
            time.sleep(random.uniform(2, 5)) # 模拟网络延迟和反爬休眠
            # yt_id = f"UC_FAKE_{name.upper()}" 
            q.put(("UPDATE_YT_ID", {'sid': sid, 'yt_id': yt_id, 'name': name}))
        except Exception as e:
            logger.error(f"❌ 抓取 {name} 的 YouTube ID 时发生错误: {e}")
            continue # 继续处理下一个艺人
    logger.info(f"🏁 本批次 ID 抓取任务推送完毕。")



# ==========================================
# 4. 主程序入口
# ==========================================
'''
if __name__ == "__main__":
    DB_FILE = "music_agent.db"
    TASK_QUEUE = queue.Queue()

    # 初始化表
    init_db(DB_FILE)

    # 启动数据库写线程
    db_writer = DatabaseConsumer(DB_FILE, TASK_QUEUE)
    db_writer.start()

    # 启动调度器
    scheduler = BackgroundScheduler()
    
    # 设定每月 1 号执行同步
    scheduler.add_job(job_sync_spotify, 'cron', day=1, hour=3, args=[TASK_QUEUE])
    
    # 设定每周一执行 ID 填充 (每次只抓 20 个)
    scheduler.add_job(job_fill_youtube_ids, 'cron', day_of_week='mon', hour=4, args=[DB_FILE, TASK_QUEUE, 20])
    
    scheduler.start()
    
    print("🌟 Music Agent 自动同步服务已启动 (按 Ctrl+C 退出)")
    
    try:
        # 保持主进程活跃
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("👋 正在关闭服务...")
        scheduler.shutdown()
        TASK_QUEUE.put(None) # 关闭消费者线程
'''
# DB_FILE = os.getenv("DB_NAME")
# TASK_QUEUE = queue.Queue()

# # 初始化表
# init_db(DB_FILE)

# # 启动数据库写线程
# db_writer = DatabaseConsumer(DB_FILE, TASK_QUEUE)
# db_writer.daemon = False
# db_writer.start()

# job_sync_spotify(TASK_QUEUE) # 再同步 Spotify，触发全量更新和潜在的 ID 变更
# job_fill_youtube_ids(DB_FILE, TASK_QUEUE) # 先填充 ID，确保后续扫描有数据
# job_rss_scanner(DB_FILE, TASK_QUEUE)
# print("⏳ 正在写入数据库，请勿关闭...")
# print("⏳ 正在等待队列清空...")
# TASK_QUEUE.join()  # 这会阻塞主线程，直到消费者执行了足够多次的 task_done()

# # --- 关键修改点 2: 发送退出信号 (Poison Pill) ---
# # 虽然是 daemon，但手动发个 None 让它正常关闭连接更优雅
# TASK_QUEUE.put(None) 
# db_writer.join()

# print("✅ 数据同步完成，程序安全退出。")
