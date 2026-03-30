import traceback

import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv
from utils.logger_manager import log_manager

logger = log_manager.get_task_logger("SPOTIFY_SYNC")

load_dotenv()  # 从 .env 文件加载环境变量

# 1. 认证设置 (信息在 Spotify Developer Dashboard 获取)
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
    scope="user-follow-read",
    open_browser=True,
    show_dialog=True
))

def get_all_followed_artists():
    artists = []
    logger.info("开始从 Spotify 获取关注艺人列表...")
    # 第一次请求
    try:
        results = sp.current_user_followed_artists(limit=50)
        artists.extend(results['artists']['items'])
        logger.info(f"首次请求成功，获取到 {len(results['artists']['items'])} 位艺人。")
    
        # 循环分页获取剩余艺人
        while results['artists']['next']:
            try:
            # 获取最后一个艺人的 ID 作为游标
                last_id = results['artists']['cursors']['after']
                results = sp.current_user_followed_artists(limit=50, after=last_id)
                artists.extend(results['artists']['items'])
            except Exception as page_err:
                # 记录分页请求中的非致命错误，尝试继续
                logger.error(f"获取其他页spotify关注艺人列表时发生错误: {page_err}")
                break # 如果游标失效，则停止分页，返回已拿到的数据
        
        return [
            {
                'id': a['id'], 
                'name': a['name'],
                'genres': ",".join(a['genres']) if a['genres'] else "" # 转化为逗号分隔字符串
            } for a in artists
        ]
    except spotipy.exceptions.SpotifyException as e:
        logger.error(f"❌ Spotify API 认证或权限错误: {e}")
    except Exception as e:
        # 使用 traceback 记录详细堆栈到日志文件，方便离线排查
        logger.error(f"🚨 获取艺人列表时发生未知致命错误: {e}")
        logger.error(traceback.format_exc())
    return [] # 发生错误时返回空列表，确保后续流程不直接崩溃


# ==========================================
# 2. 生产者任务：spotify 关注列表同步
# ==========================================
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

# 运行获取
# followed_list = get_all_followed_artists()
# print(f"你一共关注了 {len(followed_list)} 位艺人：", followed_list)