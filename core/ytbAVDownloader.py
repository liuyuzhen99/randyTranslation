import yt_dlp
def download_step_video(song_name, output_path):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'default_search': 'ytsearch1:',
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([song_name])
    return output_path

def download_step_audio(song_name, output_path):
    
    ydl_opts = {
        # 1. 仅下载音频并转为 mp3 (速度最快)
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        
        'outtmpl': output_path,
        'default_search': 'ytsearch1:',
        'noplaylist': True,

        # 2. 核心稳定性增强 (处理 600+ 艺人的关键)
        'retries': 15,                     # 遇到网络抖动自动重试 15 次
        'fragment_retries': 15,            # 分片下载失败重试
        'ignoreerrors': True,              # 即使某个视频报错也跳过，不崩溃主程序
        'skip_download': False,            
        'wait_for_video': (5, 60),         # 应对新歌发布：如果音轨还没压好，等待 5-60 秒
        
        # 3. 规避风控与环境稳定性
        'nocheckcertificate': True,        # 忽略 SSL 证书错误（代理环境常用）
        'no_warnings': False,              # 显示警告以便排查 JS Runtime 问题
        'headers': {                       # 伪装浏览器，降低被封 IP 风险
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        },
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # ydl.download 返回 0 表示成功，非 0 表示有错误但被 ignoreerrors 捕获了
            status = ydl.download([song_name])
            if status != 0:
                print(f"⚠️ {song_name} 下载不完全或已跳过。")
    except Exception as e:
        print(f"❌ 下载模块发生致命异常: {e}")
    
    return output_path