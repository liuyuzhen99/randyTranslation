import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv

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
    # 第一次请求
    results = sp.current_user_followed_artists(limit=50)
    artists.extend(results['artists']['items'])
    
    # 循环分页获取剩余艺人
    while results['artists']['next']:
        # 获取最后一个艺人的 ID 作为游标
        last_id = results['artists']['cursors']['after']
        results = sp.current_user_followed_artists(limit=50, after=last_id)
        artists.extend(results['artists']['items'])
        
    return [
        {
            'id': a['id'], 
            'name': a['name']
        } for a in artists
    ]

# 运行获取
# followed_list = get_all_followed_artists()
# print(f"你一共关注了 {len(followed_list)} 位艺人：", followed_list)