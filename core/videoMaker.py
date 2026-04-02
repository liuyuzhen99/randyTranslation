import os
import sqlite3
import subprocess
import traceback
# 导入你的日志类实例
from utils.logger_manager import log_manager

# 初始化专门针对视频合成任务的 Logger
logger = log_manager.get_task_logger("VIDEO_RENDER")

def burn_video(video_id, db_path, task_queue):
    try:
        conn=sqlite3.connect(db_path)
        cur=conn.cursor()
        cur.execute("SELECT title, video_path, srt_path FROM videos WHERE video_id=?", (video_id,))
        title, video_path, srt_path = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.error(f"❌ 数据库查询失败: {e}")
        return False
    final_path = f"/Users/randy/Downloads/temp/{title}_{video_id}_final.mp4"
    logger.info(f"🎬 开始视频压制任务: {os.path.basename(final_path)}")
    if not os.path.exists(video_path):
        logger.error(f"❌ 压制失败：原始视频不存在 -> {video_path}")
        return False
    if not os.path.exists(srt_path):
        logger.error(f"❌ 压制失败：字幕文件不存在 -> {srt_path}")
        return False
    # 这里的 style 参考了你之前代码中的设置
    style = (
        "Fontname=PingFang SC,"
        "Fontsize=18,"
        "PrimaryColour=&H00FFFFFF,"  # 白色文字
        "OutlineColour=&H00000000,"  # 黑色描边
        "Outline=1,"
        "Shadow=0,"
        "Alignment=2,"
        "MarginV=25"
    )
    cmd = [
        'ffmpeg',
        '-i', video_path,
        '-vf', f"subtitles='{srt_path}':force_style='{style}'",
        '-c:v', 'libx264',
        '-preset', 'medium',   # 兼顾速度与画质
        '-crf', '20',          # 高质量压制
        '-c:a', 'copy',        # 音频直接复制，不损失音质
        '-y',                  # 覆盖输出
        final_path
    ]
    try:
        # 使用 subprocess.run 运行，并捕获错误信息
        logger.info("⚡ FFmpeg 进程启动，正在压制中...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"✅ 压制成功！成品已生成: {final_path}")
            task_queue.put(("UPDATE_VIDEO_RENDER", {
                'video_id': video_id,
                'final_path': final_path
            }))
            return True
        else:
            task_queue.put(("UPDATE_VIDEO_RENDER", {
                'video_id': video_id,
                'final_path': 'render_failed'
            })) 
            logger.error(f"❌ FFmpeg 压制进程返回非零状态码: {result.returncode}")
            logger.error(f"🔍 FFmpeg 错误详情: {result.stderr[-500:]}") # 只记录最后500字关键报错
            return False
            
    except FileNotFoundError:
        task_queue.put(("UPDATE_VIDEO_RENDER", {
            'video_id': video_id,
            'final_path': 'render_failed'
        })) 
        logger.error("🚨 系统未找到 ffmpeg 可执行程序，请检查是否已安装并加入 PATH。")
    except Exception as e:
        task_queue.put(("UPDATE_VIDEO_RENDER", {
            'video_id': video_id,
            'final_path': 'render_failed'
        }))
        logger.error(f"🚨 压制模块发生未预料异常: {e}")
        logger.error(traceback.format_exc())
        return False