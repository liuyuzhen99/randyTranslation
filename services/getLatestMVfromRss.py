import feedparser
from datetime import datetime, timedelta
import time
import sqlite3
import traceback
# 导入你的日志类实例
from utils.logger_manager import log_manager

# 初始化专门针对 RSS 扫描的任务 Logger
logger = log_manager.get_task_logger("RSS_SCANNER")

# ==========================================
# 1. 过滤逻辑 (保持你原有的精髓)
# ==========================================
def is_valid_mv(title, url):
    """
    判断是否为有效的 MV 内容
    """
    title = title.lower()
    url = url.lower()
    
    # 黑名单关键词
    blacklist = ["behind the scenes", "bts", "teaser", "trailer", "interview", "vlog", "live at", "documentary"]
    
    # 过滤规则：不是 Shorts 且 标题不含黑名单
    if "/shorts/" in url or "#shorts" in title:
        return False, "Shorts"
    
    for word in blacklist:
        if word in title:
            return False, f"Non-MV ({word})"
            
    return True, "Valid"

'''
def get_recent_releases(mapping, days=14):
    # 计算时间阈值
    deadline = datetime.now() - timedelta(days=days)
    new_releases = []

    for name, channel_id in mapping.items():
        print(f"📡 正在扫描 {name} 的官方频道...")
        
        # 构造 RSS 链接
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        feed = feedparser.parse(rss_url)

        for entry in feed.entries:
            # 解析发布时间 (RSS 返回的是 struct_time 格式)
            published_time = datetime.fromtimestamp(time.mktime(entry.published_parsed))
            
            if published_time > deadline:
                # 识别是否为 Shorts (简单启发式：标题包含 #shorts 或 链接包含 shorts)
                is_shorts = "#shorts" in entry.title.lower()
                
                new_releases.append({
                    "artist": name,
                    "title": entry.title,
                    "link": entry.link,
                    "published": published_time.strftime('%Y-%m-%d %H:%M'),
                    "is_shorts": is_shorts
                })
        
        # 稍微停顿，模拟人类访问频率
        time.sleep(0.5)
    filtered_releases = filter_official_mv(new_releases)
    return filtered_releases
'''
# ==========================================
# 2. 生产者任务：RSS 扫描器
# ==========================================
def job_rss_scanner(db_path, task_queue, days=14):
    """
    从数据库读取 ID 并扫描近两周 MV
    """
    logger.info(f"📡 开始扫描任务。范围: 过去 {days} 天。")
    artists = []

    try:
        # 1. 连库读取已激活且有 ID 的艺人
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # 只查活跃且有 YouTube ID 的艺人
        cur.execute("SELECT spotify_id, name, yt_channel_id FROM artists WHERE status='active' AND yt_channel_id IS NOT NULL")
        artists = cur.fetchall()
        conn.close()
        logger.info(f"✅ 成功从数据库读取 {len(artists)} 位活跃艺人。")
    except Exception as db_err:
        logger.error(f"🚨 无法读取数据库艺人列表: {db_err}")
        logger.error(traceback.format_exc())
        return
    
    deadline = datetime.now() - timedelta(days=days)
    found_count = 0
    error_count = 0

    for index, (sid, name, channel_id) in enumerate(artists):
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            feed = feedparser.parse(rss_url)
            # 检查 feed 是否包含 bozo 异常（feedparser 的内置错误标志）
            if feed.bozo:
                logger.warning(f"⚠️ 解析 {name} 的 RSS 时遇到非致命异常: {feed.bozo_exception}")
            if not feed.entries:
                continue

            for entry in feed.entries:
                try:
                    # 解析发布时间
                    pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                except (AttributeError, TypeError):
                    logger.warning(f"⏰ {name} - {entry.title[:20]}... 时间解析失败，跳过。")
                    continue

                if pub_date > deadline:
                    # 调用过滤函数
                    is_valid, reason = is_valid_mv(entry.title, entry.link)
                    
                    if is_valid:
                        video_data = {
                            'video_id': entry.yt_videoid,
                            'spotify_id': sid,
                            'artist_name': name,
                            'title': entry.title,
                            'link': entry.link,
                            'published_at': pub_date.strftime('%Y-%m-%d %H:%M:%S')
                        }
                        
                        # 推送到队列，由 DatabaseConsumer 负责去重并写入 videos 表
                        task_queue.put(("NEW_VIDEO_FOUND", video_data))
                        found_count += 1
                        logger.info(f"✨ 发现新 MV: {name} - {entry.title} video_id: {entry.yt_videoid}")
                # 定时记录进度，防止 600+ 艺人扫描时看起来像卡死了
                if (index + 1) % 50 == 0:
                    logger.info(f"📊 扫描进度: {index + 1}/{len(artists)} 位艺人已检查...")
            
            # 频率控制：623 个艺人如果请求太快会被 YouTube 临时屏蔽
            time.sleep(0.3) 
            
        except Exception as e:
            error_count += 1
            logger.error(f"⚠️ 扫描艺人 {name} (ID: {channel_id}) 时出错: {e}")
            if error_count > 20:
                logger.critical("🚨 连续报错次数过多，可能已被 YouTube 封禁 IP，停止扫描任务！")
                break

    logger.info(f"🏁 扫描任务结束。共推送 {found_count} 个潜在 MV，过程中发生 {error_count} 次错误。")

def job_rss_scanner_channelID(db_path, task_queue, days, channel_id):
    """
    从数据库读取 ID 并扫描近两周 MV
    """
    logger.info(f"📡 开始扫描任务。范围: 过去 {days} 天。")
    artists = []

    try:
        # 1. 连库读取已激活且有 ID 的艺人
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        # 只查活跃且有 YouTube ID 的艺人
        cur.execute("SELECT spotify_id, name, yt_channel_id FROM artists WHERE status='active' AND yt_channel_id = ?", (channel_id,))
        artists = cur.fetchall()
        conn.close()
        logger.info(f"✅ 成功从数据库读取 {len(artists)} 位活跃艺人。")
    except Exception as db_err:
        logger.error(f"🚨 无法读取数据库艺人列表: {db_err}")
        logger.error(traceback.format_exc())
        return
    
    deadline = datetime.now() - timedelta(days=days)
    found_count = 0
    error_count = 0

    for index, (sid, name, channel_id) in enumerate(artists):
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            feed = feedparser.parse(rss_url)
            # 检查 feed 是否包含 bozo 异常（feedparser 的内置错误标志）
            if feed.bozo:
                logger.warning(f"⚠️ 解析 {name} 的 RSS 时遇到非致命异常: {feed.bozo_exception}")
            if not feed.entries:
                continue

            for entry in feed.entries:
                try:
                    # 解析发布时间
                    pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                except (AttributeError, TypeError):
                    logger.warning(f"⏰ {name} - {entry.title[:20]}... 时间解析失败，跳过。")
                    continue

                if pub_date > deadline:
                    # 调用过滤函数
                    is_valid, reason = is_valid_mv(entry.title, entry.link)
                    
                    if is_valid:
                        video_data = {
                            'video_id': entry.yt_videoid,
                            'spotify_id': sid,
                            'artist_name': name,
                            'title': entry.title,
                            'link': entry.link,
                            'published_at': pub_date.strftime('%Y-%m-%d %H:%M:%S')
                        }
                        
                        # 推送到队列，由 DatabaseConsumer 负责去重并写入 videos 表
                        task_queue.put(("NEW_VIDEO_FOUND", video_data))
                        found_count += 1
                        logger.info(f"✨ 发现新 MV: {name} - {entry.title} video_id: {entry.yt_videoid}")
                # 定时记录进度，防止 600+ 艺人扫描时看起来像卡死了
                if (index + 1) % 50 == 0:
                    logger.info(f"📊 扫描进度: {index + 1}/{len(artists)} 位艺人已检查...")
            
            # 频率控制：623 个艺人如果请求太快会被 YouTube 临时屏蔽
            time.sleep(0.3) 
            
        except Exception as e:
            error_count += 1
            logger.error(f"⚠️ 扫描艺人 {name} (ID: {channel_id}) 时出错: {e}")
            if error_count > 20:
                logger.critical("🚨 连续报错次数过多，可能已被 YouTube 封禁 IP，停止扫描任务！")
                break

    logger.info(f"🏁 扫描任务结束。共推送 {found_count} 个潜在 MV，过程中发生 {error_count} 次错误。")