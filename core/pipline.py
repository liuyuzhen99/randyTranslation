
import os
import sqlite3
from core.ytbAVDownloader import download_step_audio, download_step_video
from core.audioTranscriber import SeparateTranscriber
from core.aiAuditor import MusicAuditor
from core.aiTranslator import Translator
from core.videoMaker import burn_video
from utils.logger_manager import log_manager

# 初始化专门针对流水线任务的 Logger
logger = log_manager.get_task_logger("PIPELINE")

def pipeline_run(title):
        try:
            # A. 下载模块 (core/ytbAVDownloader.py)
            logger.info(f"⏳ [Step 1/5] 正在下载音频: {title}")
            audio_info = download_step_audio(title, f"/Users/randy/Downloads/temp/{title}") 

            # B. 转录模块 (core/audioTranscriber.py)
            logger.info(f"⏳ [Step 2/5] 正在提取歌词 (Whisper)...")
            transcriber = SeparateTranscriber()
            full_data, english_text = transcriber.transcribe_step(audio_info)
            
            # C. 审计模块 (core/aiAuditor.py)
            # logger.info(f"⏳ [Step 3/5] 正在进行 AI 审计...")
            # auditor = MusicAuditor(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=os.getenv("DEEPSEEK_BASE_URL"))
            # audit_res = auditor.ai_audit(english_text)
            
            # if audit_res['decision'] != 'pass':
            #     logger.warning("🚫 审计未通过：风格不符，跳过。")
            #     return

            # D. 翻译模块 (core/aiTranslator.py)
            logger.info(f"⏳ [Step 4/5] 正在调用 翻译模型翻译...")
            translator =Translator(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=os.getenv("DEEPSEEK_BASE_URL"))
            srt_path = f"/Users/randy/Downloads/temp/{title}_cn_en.srt"
            output_file = translator.generate_bilingual_srt(full_data, english_text, srt_path)

            # E. 下载视频
            logger.info(f"⏳ [Step 5/5] 正在下载视频: {title}")
            video_path = download_step_video(title, f"/Users/randy/Downloads/temp/{title}_video")

            # E. 压制模块 (core/videoMaker.py)
            logger.info(f"⏳ [Step 5/5] 正在合成最终视频 (FFmpeg)...")
            final_path = f"/Users/randy/Downloads/temp/{title}_final.mp4"
            burn_video(video_path, output_file, final_path)
            logger.info(f"🎉 视频 {title} 生产完成！")

        except Exception as e:
            err_msg = str(e)
            logger.error(f"🚨 流水线崩溃: {err_msg}")


def run_full_pipeline(db_name, q, video_id):
        """
        [重点]：串联 core 逻辑的生产流水线
        """
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT video_id, title FROM videos WHERE video_id=? AND processed_status='new'", (video_id,))
        video_info = cursor.fetchone()
        conn.close()
        if not video_info:
            logger.warning(f"⚠️ 视频 {video_id} 不存在或已处理。")
            return
        title = video_info[1]
        # 1. 更新状态为处理中
        q.put(("UPDATE_VIDEO_STATUS", {'processed_status': 'processing', 'video_id': video_id}))
        
        try:
            # A. 下载模块 (core/ytbAVDownloader.py)
            logger.info(f"⏳ [Step 1/5] 正在下载音频: {title}")
            audio_info = download_step_audio(title, f"/Users/randy/Downloads/temp/{title}.mp3") 

            # B. 转录模块 (core/audioTranscriber.py)
            logger.info(f"⏳ [Step 2/5] 正在提取歌词 (Whisper)...")
            transcriber = SeparateTranscriber()
            full_data, english_text = transcriber.transcribe_step(audio_info)
            
            # C. 审计模块 (core/aiAuditor.py)
            logger.info(f"⏳ [Step 3/5] 正在进行 AI 审计...")
            auditor = MusicAuditor(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=os.getenv("DEEPSEEK_BASE_URL"))
            audit_res = auditor.ai_audit(english_text)
            
            if audit_res['decision'] != 'pass':
                logger.warning("🚫 审计未通过：风格不符，跳过。")
                q.put(("UPDATE_VIDEO_STATUS", 
                       {'processed_status': 'skipped',
                         'video_id': video_id,
                         'final_path': None,
                         'srt_path': None}))
                return

            # D. 翻译模块 (core/aiTranslator.py)
            logger.info(f"⏳ [Step 4/5] 正在调用 翻译模型翻译...")
            translator =Translator()
            srt_path = f"/Users/randy/Downloads/temp/{title}_cn_en.srt"
            output_file = translator.generate_bilingual_srt(full_data, english_text, srt_path)

            # E. 下载视频
            logger.info(f"⏳ [Step 5/5] 正在下载视频: {title}")
            video_path = download_step_video(title, f"/Users/randy/Downloads/temp/{title}_video.mp4")

            # E. 压制模块 (core/videoMaker.py)
            logger.info(f"⏳ [Step 5/5] 正在合成最终视频 (FFmpeg)...")
            final_path = f"/Users/randy/Downloads/temp/{title}_final.mp4"
            burn_video(video_path, output_file, final_path)
            q.put(("UPDATE_VIDEO_RESULT", {
                'processed_status': 'completed',
                'video_id': video_id,
                'final_path': final_path,
                'srt_path': srt_path
            }))
            logger.info(f"🎉 视频 {video_id} 生产完成！")

        except Exception as e:
            err_msg = str(e)
            logger.error(f"🚨 流水线崩溃: {err_msg}")
            q.put(("UPDATE_VIDEO_STATUS", {
                'processed_status': 'failed',
                'video_id': video_id,
                'final_path': None,
                'srt_path': None
            }))