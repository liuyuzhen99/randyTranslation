import sqlite3
import threading
import queue
from datetime import datetime
from dotenv import load_dotenv
import traceback
from utils.logger_manager import log_manager
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
        self.logger = log_manager.get_task_logger("DB_MANAGER")
        self._init_db()
        
        # 初始化数据库消费者线程
        self.consumer = DatabaseConsumer(self.db_path, self.task_queue)
        self.consumer.start()

    def _init_db(self):
        self.logger.info(f"🏗️ 正在初始化数据库: {self.db_path}")
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
                    bpm REAL,
                    energy REAL,
                    word_density REAL,
                    lyrics TEXT,
                    error_msg TEXT,
                    FOREIGN KEY (spotify_id) REFERENCES artists (spotify_id)
                )
            ''')
            # 行级字幕对齐表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subtitles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT,
                    line_index INTEGER,
                    start_time REAL,    -- 存储秒数，方便后续计算
                    end_time REAL,
                    en_text TEXT,
                    zh_text TEXT,
                    status TEXT DEFAULT 'raw', -- raw, translated, confirmed
                    FOREIGN KEY (video_id) REFERENCES videos (video_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_proc_status ON videos(processed_status);')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sub_vid ON subtitles(video_id);')
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.critical(f"🚨 数据库初始化失败: {e}")
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
        self.logger = log_manager.get_task_logger("DB_CONSUMER")

    def run(self):
        self.logger.info("🚀 生产流水线守护线程已就绪...")
        try:
            conn = sqlite3.connect(self.db_name, timeout=30)
            conn.execute('PRAGMA journal_mode=WAL;') # 必开，支持并发读
            conn.execute('PRAGMA busy_timeout=10000;')   # 推荐 5~15 秒，根据你的写事务时长调整
            conn.execute('PRAGMA synchronous=NORMAL;')   # 可选，平衡速度与安全
            cursor = conn.cursor()
        except Exception as e:
            self.logger.critical(f"❌ 数据库连接失败，写线程终止: {e}")
            return
        
        while True:
            task = self.task_queue.get()
            if task is None: 
                self.task_queue.task_done()
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
                    #         INSERT INTO artists (spotify_id, name, genres, status, last_sync_at)
                    #         VALUES (?, ?, ?, 'active', ?)
                    #         ON CONFLICT(spotify_id) DO UPDATE SET 
                    #             status='active', 
                    #             last_sync_at=excluded.last_sync_at,
                    #             name=excluded.name
                    #     ''', (a['id'], a['name'], a['genres'], now))
                        
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
                        self.logger.info(f"🆕 发现新 MV 并存库: 歌曲名称{data['title']} video_id:{data['video_id']}")
                elif action == "UPDATE_VIDEO_STATUS":
                    if data['processed_status'] not in ['new', 'processing', 'completed', 'skipped', 'failed']:
                        self.logger.warning(f"⚠️ 收到未知的 processed_status: {data['processed_status']}，已忽略。")
                        continue
                    if data['processed_status'] == 'processing':
                        cursor.execute('''
                            UPDATE videos SET processed_status = 'processing' WHERE video_id = ?
                        ''', (data['video_id'],))
                    else:
                        cursor.execute('''
                            UPDATE videos SET 
                            processed_status=?, 
                            final_video_path=?, 
                            srt_path=? 
                        WHERE video_id=?
                        ''', (data['processed_status'], data['final_path'], data['srt_path'], data['video_id']))
                    self.logger.info(f"✅ 视频处理结果已更新到数据库: {data['video_id']}")
                elif action == "UPDATE_VIDEO_LYRICS":
                    cursor.execute('''
                        UPDATE videos SET lyrics = ?, processed_status = 'transcribed' WHERE video_id = ?
                    ''', (data['lyrics'], data['video_id']))
                    self.logger.info(f"✅ 视频歌词已更新到数据库: video_id:{data['video_id']}")
                elif action == "UPDATE_VIDEO_ANALYSIS":
                    cursor.execute('''
                        UPDATE videos SET 
                            bpm = ?, 
                            energy = ?, 
                            word_density = ?,
                            processed_status = 'analyzed'
                        WHERE video_id = ?
                    ''', (data['bpm'], data['energy'], data['word_density'], data['video_id']))
                    self.logger.info(f"✅ 视频分析结果已更新到数据库: 歌曲名称{data['title']} video_id:{data['video_id']}")
                elif action == "INIT_SUBTITLES":
                    """批量初始化行级字幕表"""
                    video_id = data['video_id']
                    segments = data['segments']
                    
                    # 1. 先清理该视频可能存在的旧字幕（支持重跑幂等性）
                    cursor.execute("DELETE FROM subtitles WHERE video_id = ?", (video_id,))
                    
                    # 2. 准备批量插入数据
                    # 对应表结构: video_id, line_index, start_time, end_time, en_text, status
                    insert_data = [
                        (video_id, i, s['start'], s['end'], s['text'], 'raw')
                        for i, s in enumerate(segments)
                    ]
                    
                    cursor.executemany('''
                        INSERT INTO subtitles (
                            video_id, line_index, start_time, end_time, en_text, status
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    ''', insert_data)
                    
                    self.logger.info(f"✨ Subtitles 已初始化: {video_id} (共 {len(insert_data)} 行)")
                elif action == "UPDATE_CH_SUBTITLES":
                    # data 此时是一个由 Translator 传过来的 list, 包含多个 dict
                    if not data :
                        self.logger.warning("⚠️ UPDATE_CH_SUBTITLES 接收到的数据格式非法或为空。")
                        continue
                    
                    # 1. 提取视频 ID（用于日志展示）
                    video_id = data['subtitles_to_db'][0].get('video_id', 'Unknown')
                    srt_path = data['srt_path']
                    # 2. 构造 executemany 所需的参数元组列表
                    # SQL 语句中的顺序是：SET zh_text = ?, status = 'translated' WHERE video_id = ? AND line_index = ?
                    # 所以元组顺序必须是 (zh_text, video_id, line_index)
                    update_params = [
                        (item['zh_text'], item['video_id'], item['line_index'])
                        for item in data['subtitles_to_db']
                    ]
                    # 3. 执行批量更新
                    cursor.executemany('''
                        UPDATE subtitles 
                        SET zh_text = ?, status = 'translated' 
                        WHERE video_id = ? AND line_index = ?
                    ''', update_params)
                    cursor.execute('''
                        UPDATE videos SET srt_path = ?, processed_status = 'translated' WHERE video_id = ?
                    ''', (srt_path, video_id))
                    self.logger.info(f"✅ [批量回填] 视频 {video_id} 的译文已存库 (共 {len(update_params)} 行)")
                elif action == "UPDATE_VIDEO_AUDIT":
                    cursor.execute('''
                        UPDATE videos SET 
                            audit_score = ?, 
                            processed_status = ?
                        WHERE video_id = ?
                    ''', (data['score'], data['decision'], data['video_id']))
                    self.logger.info(f"✅ 视频审计结果已更新到数据库: 歌曲名称{data['title']} video_id:{data['video_id']}")
                elif action == "UPDATE_VIDEO_DOWNLOAD":
                    cursor.execute('''
                        UPDATE videos SET 
                            local_video_path = ?
                        WHERE video_id = ?
                    ''', (data['download_path'], data['video_id']))
                    self.logger.info(f"✅ 视频下载状态已更新到数据库: {data['video_id']}")
                elif action == "UPDATE_VIDEO_RENDER":
                    cursor.execute('''
                        UPDATE videos SET 
                            final_video_path = ?, 
                            processed_status = 'completed' 
                        WHERE video_id = ?
                    ''', (data['final_path'], data['video_id']))
                    self.logger.info(f"✅ 视频渲染状态已更新到数据库: {data['video_id']}")
                else:
                    self.logger.warning(f"⚠️ 收到未知的写操作请求: [{action}]，已忽略。")
                    continue
                                
                conn.commit()
            except Exception as e:
                self.logger.error(f"❌ 数据库写入操作 [{action}] 失败: {e}")
                self.logger.error(traceback.format_exc())
                conn.rollback()
                break  # 非 locked 错误直接结束本次任务
            finally:
                self.task_queue.task_done()
        conn.close()
        return None

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

# print("✅ 数据同步完成，程序安全退出。") # 设定每月 1 号执行同步
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

