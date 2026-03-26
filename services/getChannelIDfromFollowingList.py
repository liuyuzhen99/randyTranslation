import traceback
import sqlite3
from DrissionPage import ChromiumOptions, ChromiumPage
import time
import urllib.parse
from utils.logger_manager import log_manager
import random

# 初始化针对 YouTube 爬虫的任务 Logger
logger = log_manager.get_task_logger("YT_CHANNEL_ID")

def fetch_youtube_channel_ids(artist_list):
    if not artist_list:
        logger.warning("传入的艺人列表为空，取消执行。")
        return {}
    logger.info(f"🚀 开始爬取任务，共计 {len(artist_list)} 位艺人")
    try:
        # 初始化浏览器（建议使用 headless 模式提高速度）
        # page = ChromiumPage()
        co = ChromiumOptions()
        co.headless(True)  # 开启无头模式，不弹出浏览器窗口
        co.incognito(True) # 开启无痕模式，减少干扰
        page = ChromiumPage(co)
    except Exception as init_err:
        logger.error(f"🚨 浏览器启动失败: {init_err}")
        logger.error(traceback.format_exc()) # 记录详细堆栈
        return {}

    results = {}
    for index, artist in enumerate(artist_list):
        try:
            logger.info(f"[{index+1}/{len(artist_list)}] 🔍 正在查询: {artist}")
            # 构造搜索 URL (sp 参数确保只搜索频道)
            query = urllib.parse.quote(f"{artist} official")
            search_url = f"https://www.youtube.com/results?search_query={query}&sp=EgIQAg%253D%253D"
            
            page.get(search_url)
            
            # 定位第一个频道结果
            # YouTube 的搜索结果中，主频道链接通常在 id="main-link" 的 <a> 标签里
            channel_link_ele = page.ele('xpath://a[@id="main-link"]', timeout=5)
            if not channel_link_ele:
                logger.warning(f"❌ 未找到匹配频道: {artist}")
                continue
            channel_href = channel_link_ele.attr('href') # 可能是 /@artist 或 /channel/UC...
                
            # 进入频道主页以获取最准确的 ID
            page.get(f"{channel_href}")
                
                # 从元数据中提取真正的 Channel ID (UC...)
                # 这是最稳妥的方法，不受自定义后缀(Handle)影响
            meta_tag = page.ele('xpath://meta[@itemprop="identifier"]', timeout=3)
                
            if meta_tag:
                channel_id = meta_tag.attr('content')
                results[artist] = channel_id
                logger.info(f"✅ 匹配成功: {artist} -> {channel_id}")
            else:
                logger.warning(f"⚠️ 页面加载成功但无法提取 meta identifier: {artist}")
            
            # 适当休眠，防止被识别为爬虫
            time.sleep(2)
            
        except Exception as e:
            # 记录循环中的单个失败，不影响整体流程
            logger.error(f"❗ 处理 {artist} 时发生异常: {e}")
            # 如果出现机器人验证，可以在此处通过日志预警
            if "captcha" in page.html.lower():
                logger.critical("🚨 检测到 YouTube 机器人验证（CAPTCHA），建议停止爬虫或更换 IP！")
                break 
            continue
    # 6. 任务结束关闭浏览器
    try:
        page.quit()
        logger.info(f"🏁 任务结束，成功匹配 {len(results)}/{len(artist_list)} 个频道")
    except Exception as quit_err:
        logger.error(f"关闭浏览器时报错: {quit_err}")

    return results

# ==========================================
# 2. 生产者任务：youtube ID 抓取
# ==========================================
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

# 测试运行
'''
test_artists = ["Baby Keem", "Offset"]
# 捕获顶层错误确保主程序不崩溃
try:
    mapping = fetch_youtube_channel_ids(test_artists)
    print(f"爬取结果预览: {mapping}")
except Exception as main_err:
    logger.critical(f"脚本运行中断: {main_err}")
'''