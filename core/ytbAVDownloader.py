import os

import yt_dlp
import traceback
# 导入你的日志类实例
from utils.logger_manager import log_manager

# 初始化专门针对下载任务的 Logger
logger = log_manager.get_task_logger("DOWNLOADER")

def download_step_video(song_name, output_path):
    logger.info(f"🎬 开始搜索并下载视频: {song_name}")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'default_search': 'ytsearch1:',
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            status = ydl.download([song_name])
        # 检查文件是否真的生成了
        if status == 0 and os.path.exists(output_path):
            logger.info(f"✅ 视频下载成功: {output_path}")
        else:
            logger.warning(f"⚠️ 视频下载状态异常(Status:{status})，或文件未找到: {output_path}")
    except Exception as e:
        logger.error(f"🚨 视频下载模块发生致命异常: {e}")
        logger.error(traceback.format_exc())
    return output_path

def download_step_audio(song_name, output_path):
    logger.info(f"🎧 开始搜索并下载音频: {song_name}")
    
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
        'logger': logger,                   # 将 yt-dlp 的日志直接输出到我们的 Logger，方便统一管理
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # ydl.download 返回 0 表示成功，非 0 表示有错误但被 ignoreerrors 捕获了
            status = ydl.download([song_name])
            if status == 0 and os.path.exists(output_path):
                logger.info(f"✅ 音频处理完成: {output_path}")
            elif status != 0:
                logger.error(f"❌ {song_name} 下载失败，yt-dlp 返回错误代码: {status}")
            else:
                logger.warning(f"⚠️ 下载完成但未在预期路径发现文件: {output_path}")
    except yt_dlp.utils.DownloadError as de:
        logger.error(f"🛑 yt-dlp 下载错误: {de}")
    except Exception as e:
        logger.error(f"🚨 下载模块发生未预料的致命异常: {e}")
        logger.error(traceback.format_exc())
    
    return output_path