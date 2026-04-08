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
from core.ytbAVDownloader import download_step_video, download_step_audio
from core.videoMaker import burn_video
from dotenv import load_dotenv
from core.pipline import pipeline_run, run_full_pipeline
from core.aiAuditor import MusicAuditor
from data.auditorVectorDatabase import MusicVectorCommander
from services.getChannelIDfromFollowingList import fetch_youtube_channel_ids, job_fill_youtube_ids
from services.getLatestMVfromRss import job_rss_scanner, job_rss_scanner_channelID
from services.getSpotifyFollowingList import get_all_followed_artists, job_sync_spotify
from data.databaseManager import DatabaseManager, DatabaseConsumer
from data.translatorVectorDatabase import TranslationVectorManager

load_dotenv()  # 从 .env 文件加载环境变量

# pipeline_run('Business & Personal')

# output_path = download_step_video("Poor Thang", "/Users/randy/Downloads/poor_thang_video.mp4")
# burn_video(output_path,"/Users/randy/Downloads/poor_thang_output.srt","/Users/randy/Downloads/poor_thang_final.mp4")

# transcriber = SeparateTranscriber()
# auditor = MusicAuditor(base_url=os.getenv("DEEPSEEK_BASE_URL"),api_key=os.getenv("DEEPSEEK_API_KEY"))
translator = Translator(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url=os.getenv("DEEPSEEK_BASE_URL"))
# full_data, english_texts = transcriber.transcribe_step("/Users/randy/Downloads/temp/Business & Personal.mp3")
# print(english_texts)
# result = auditor.ai_audit(english_texts)
# output_file = translator.generate_bilingual_srt(full_data, english_texts, "/Users/randy/Downloads/poor_thang_output.srt")

dataManager = DatabaseManager("data/music_data.db")
# transcriber = SeparateTranscriber()
# musicVector = MusicVectorCommander(db_path="./data/chroma_db")  # 初始化向量数据库管理器
lyricsVector = TranslationVectorManager(db_path="./data/chroma_db")  # 初始化翻译向量数据库管理器
translator.generate_bilingual_srt('3aIjM1YGOO8', dataManager.task_queue, dataManager.db_path, lyricsVector)  # 直接在测试中调用翻译，并传入数据库路径和任务队列
# result = lyricsVector.query_song_level_style('wiALRpD0Ztg', dataManager.db_path, n_results=3)  # 测试基于当前视频的智能搜索功能

# download_step_audio('KHpA7_c3u1Y',dataManager.task_queue,"J. Cole - Bombs in the Ville/Hit the Gas (Official Audio)")
# job_fill_youtube_ids(dataManager.db_path, dataManager.task_queue, batch_size=50)
# job_rss_scanner_channelID(dataManager.db_path, dataManager.task_queue, 60, "UCnc6db-y3IU7CkT_yeVXdVg")
# full_data, english_texts = transcriber.transcribe_step('KHpA7_c3u1Y',dataManager.db_path, dataManager.task_queue)  # 直接在测试中调用转录，并传入数据库路径和任务队列
# transcriber.analyze_audio('wiALRpD0Ztg',dataManager.db_path, dataManager.task_queue, english_texts)  # 直接在测试中调用分析，并传入数据库路径和任务队列
# musicVector.sync_to_vector_db(dataManager.db_path, "frYF8yvYZrc")  # 测试同步特定 video_id 的数据到向量数据库
# result = musicVector.smart_search_by_current_video(dataManager.db_path, "wiALRpD0Ztg", 1)  # 测试基于当前视频的智能搜索功能
# remark = auditor.ai_audit_with_context('wiALRpD0Ztg', dataManager.db_path, result)  # 测试 AI 审计功能，传入数据库路径和向量搜索结果
dataManager.task_queue.join()  # 这会阻塞主线程，直到消费者执行了足够多次的 task_done()
# # --- 关键修改点 2: 发送退出信号 (Poison Pill) ---
# # 虽然是 daemon，但手动发个 None 让它正常关闭连接更优雅
dataManager.task_queue.put(None) 


# download_step_audio('wiALRpD0Ztg', "Kendrick Lamar - man at the garden (Official Audio)")