from DrissionPage import ChromiumOptions, ChromiumPage
import time
import urllib.parse
from getSpotifyFollowingList import get_all_followed_artists

# followed_list = get_all_followed_artists()

def fetch_youtube_channel_ids(artist_list):
    # 初始化浏览器（建议使用 headless 模式提高速度）
    # page = ChromiumPage()
    co = ChromiumOptions()
    co.headless(True)  # 开启无头模式，不弹出浏览器窗口
    co.incognito(True) # 开启无痕模式，减少干扰
    results = {}
    page = ChromiumPage(co)

    for artist in artist_list:
        try:
            print(f"🔍 正在查询艺人: {artist}...")
            
            # 构造搜索 URL (sp 参数确保只搜索频道)
            query = urllib.parse.quote(f"{artist} official")
            search_url = f"https://www.youtube.com/results?search_query={query}&sp=EgIQAg%253D%253D"
            
            page.get(search_url)
            
            # 定位第一个频道结果
            # YouTube 的搜索结果中，主频道链接通常在 id="main-link" 的 <a> 标签里
            channel_link_ele = page.ele('xpath://a[@id="main-link"]', timeout=5)
            
            if channel_link_ele:
                channel_href = channel_link_ele.attr('href') # 可能是 /@artist 或 /channel/UC...
                
                # 进入频道主页以获取最准确的 ID
                page.get(f"{channel_href}")
                
                # 从元数据中提取真正的 Channel ID (UC...)
                # 这是最稳妥的方法，不受自定义后缀(Handle)影响
                meta_tag = page.ele('xpath://meta[@itemprop="identifier"]', timeout=3)
                
                if meta_tag:
                    channel_id = meta_tag.attr('content')
                    results[artist] = channel_id
                    print(f"✅ 匹配成功: {artist} -> {channel_id}")
                else:
                    print(f"⚠️ 找到频道但无法提取 ID: {artist}")
            else:
                print(f"❌ 未找到匹配频道: {artist}")
            
            # 适当休眠，防止被识别为爬虫
            time.sleep(2)
            
        except Exception as e:
            print(f"❗ 处理 {artist} 时出错: {e}")
            continue

    page.quit()
    return results

# 测试运行
# spotify_followed_artists = ["Baby Keem", "Offset", "OT the Real", "latto"]
# mapping = fetch_youtube_channel_ids(spotify_followed_artists)
# print("\n最终结果:")
# for artist, channel_id in mapping.items():
#     print(f"{artist}: {channel_id}")