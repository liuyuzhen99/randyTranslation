import sys
import os

# 获取 test.py 所在目录的绝对路径
current_dir = os.path.dirname(os.path.abspath(__file__))

# 添加项目根目录到 sys.path（推荐方式）
project_root = os.path.abspath(os.path.join(current_dir, '..'))
sys.path.insert(0, project_root)

import os
from core.audioTranscriber import SeparateTranscriber
from core.aiTranslator import Translator
from core.ytbAVDownloader import download_step_video
from core.videoMaker import burn_video
from dotenv import load_dotenv
from core.pipline import pipeline_run, run_full_pipeline
from core.aiAuditor import MusicAuditor
from data.auditorVectorDatabase import MusicVectorCommander
from services.getChannelIDfromFollowingList import fetch_youtube_channel_ids, job_fill_youtube_ids
from services.getLatestMVfromRss import job_rss_scanner, job_rss_scanner_channelID
from services.getSpotifyFollowingList import get_all_followed_artists, job_sync_spotify
from data.databaseManager import DatabaseManager, DatabaseConsumer

load_dotenv()  # 从 .env 文件加载环境变量

# pipeline_run('Business & Personal')

# output_path = download_step_video("Poor Thang", "/Users/randy/Downloads/poor_thang_video.mp4")
# burn_video(output_path,"/Users/randy/Downloads/poor_thang_output.srt","/Users/randy/Downloads/poor_thang_final.mp4")

# transcriber = SeparateTranscriber()
# auditor = MusicAuditor(base_url=os.getenv("DEEPSEEK_BASE_URL"),api_key=os.getenv("DEEPSEEK_API_KEY"))
# translator = Translator(api_key=os.getenv("DEEP_SEEK_API_KEY"), base_url=os.getenv("DEEP_SEEK_BASE_URL"))
# full_data, english_texts = transcriber.transcribe_step("/Users/randy/Downloads/temp/Business & Personal.mp3")
# print(english_texts)
# result = auditor.ai_audit(english_texts)
# output_file = translator.generate_bilingual_srt(full_data, english_texts, "/Users/randy/Downloads/poor_thang_output.srt")

dataManager = DatabaseManager("data/music_data.db")
dataConsumer = DatabaseConsumer(dataManager.db_path,dataManager.task_queue)
dataConsumer.start()
# job_sync_spotify(dataManager.task_queue)
# job_fill_youtube_ids(dataManager.db_path, dataManager.task_queue, batch_size=50)
job_rss_scanner_channelID(dataManager.db_path, dataManager.task_queue, 60, "UCnc6db-y3IU7CkT_yeVXdVg")
dataManager.task_queue.join()  # 这会阻塞主线程，直到消费者执行了足够多次的 task_done()
# # --- 关键修改点 2: 发送退出信号 (Poison Pill) ---
# # 虽然是 daemon，但手动发个 None 让它正常关闭连接更优雅
dataManager.task_queue.put(None) 
dataConsumer.join()

print("✅ 数据同步完成，程序安全退出。") 