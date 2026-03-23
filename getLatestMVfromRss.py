import feedparser
from datetime import datetime, timedelta
import time
import sqlite3

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
    print(f"📡 [{datetime.now()}] 开始扫描 YouTube 频道更新...")
    
    # 1. 连库读取已激活且有 ID 的艺人
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # 只查活跃且有 YouTube ID 的艺人
    cur.execute("SELECT spotify_id, name, yt_channel_id FROM artists WHERE status='active' AND yt_channel_id IS NOT NULL")
    artists = cur.fetchall()
    conn.close()

    deadline = datetime.now() - timedelta(days=days)
    found_count = 0

    for sid, name, channel_id in artists:
        # print(f"🔍 正在检查: {name}...")
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            feed = feedparser.parse(rss_url)
            if not feed.entries:
                continue

            for entry in feed.entries:
                # 解析发布时间
                pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                
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
                    # else:
                    #    print(f"  🚫 过滤: {name} - {entry.title[:30]}... ({reason})")
            
            # 频率控制：623 个艺人如果请求太快会被 YouTube 临时屏蔽
            time.sleep(0.3) 
            
        except Exception as e:
            print(f"⚠️ 扫描 {name} 时出错: {e}")

    print(f"✨ 扫描任务结束，向队列推送了 {found_count} 个潜在 MV。")

# 执行扫描
# recent_videos = get_recent_releases(artist_mapping)

# 打印结果
# print(f"\n✨ 在过去 14 天内发现 {len(recent_videos)} 个更新：")
# for vid in recent_videos:
#     type_tag = "[Shorts]" if vid['is_shorts'] else "[MV/Video]"
#     print(f"- {vid['published']} | {vid['artist']} | {type_tag} {vid['title']}")
#     print(f"  🔗 {vid['link']}")