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

load_dotenv()  # 从 .env 文件加载环境变量

pipeline_run('Business & Personal')

# output_path = download_step_video("Poor Thang", "/Users/randy/Downloads/poor_thang_video.mp4")
# burn_video(output_path,"/Users/randy/Downloads/poor_thang_output.srt","/Users/randy/Downloads/poor_thang_final.mp4")

# transcriber = SeparateTranscriber()
# auditor = MusicAuditor(base_url=os.getenv("DEEPSEEK_BASE_URL"),api_key=os.getenv("DEEPSEEK_API_KEY"))
# translator = Translator(api_key=os.getenv("DEEP_SEEK_API_KEY"), base_url=os.getenv("DEEP_SEEK_BASE_URL"))
# full_data, english_texts = transcriber.transcribe_step("/Users/randy/Downloads/temp/Business & Personal.mp3")
# print(english_texts)
# result = auditor.ai_audit(english_texts)
# output_file = translator.generate_bilingual_srt(full_data, english_texts, "/Users/randy/Downloads/poor_thang_output.srt")