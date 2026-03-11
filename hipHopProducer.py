import os
import subprocess
import re
from faster_whisper import WhisperModel
from llama_cpp import Llama
import yt_dlp
import tempfile
import shutil
import torch


# ================= 配置区 =================
# 修改为你本地 GGUF 模型的实际路径
# MODEL_PATH = "/Users/randy/.cache/huggingface/hub/models--Randyliu99--qwen2.5-7b-jcole-gguf/snapshots/main/qwen2.5-7b-instruct-q4_k_m.gguf"
# 视频输出文件名
VIDEO_INPUT = "input_video.mp4"
VIDEO_OUTPUT = "/Users/randy/Downloads/final_hiphop_mv.mp4"
# ==========================================

# --- 辅助函数：时间格式化 ---
def format_timestamp(seconds: float):
    millis = int((seconds - int(seconds)) * 1000)
    td_sec = int(seconds)
    td_min, td_sec = divmod(td_sec, 60)
    td_hour, td_min = divmod(td_min, 60)
    return f"{td_hour:02}:{td_min:02}:{td_sec:02},{millis:03}"

class HipHopAutoProject:
    def __init__(self):
        print("🏗️ 正在初始化模型...")
        # 加载 Qwen 微调模型 (llama-cpp-python)
        self.llm = Llama.from_pretrained(
            repo_id="Randyliu99/qwen2.5-7b-jcole-gguf",
            filename="Qwen2.5-7B-Instruct.Q4_K_M.gguf",
            n_ctx=2048,  # 设置为 2048 或更高，解决 1040 报错
            n_gpu_layers=-1 # 如果有显卡/金属加速，记得开启
        )
        self.whisper = WhisperModel("medium", device="cpu", compute_type="int8")
        self.temp_dir = None
        print("✅ 模型加载完成！")

    def download_step(self, song_name, output_path=VIDEO_INPUT):
        print(f"📥 正在搜索并下载: {song_name}...")
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': output_path,
            'default_search': 'ytsearch1:',
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([song_name])
        return output_path

    def transcribe_step(self, video_path, audio_path):
        print("正在提取音轨...")
        subprocess.run([
            'ffmpeg', '-i', video_path, 
            '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', 
            audio_path, '-y'
        ], check=True)

        # --------------------------
        # add demucs分离人声和音轨
        demucs_cmd = [
            "python3", "-m", "demucs",
            "-o", self.temp_dir,
            "--two-stems", "vocals",
            "-d", "mps",  # 既然你命令行 mps 成功了，这里保持一致
            audio_path
        ]
        # 增加 check=True 和 capture_output=True 来获取错误细节
        result = subprocess.run(demucs_cmd, check=True, capture_output=True, text=True)
        audio_path = os.path.join(self.temp_dir, "htdemucs", "temp_audio", "vocals.wav")  # 注意路径要和 demucs 输出一致
        # --------------------------

        print("🎙️ 正在提取音轨并识别 (Faster-Whisper)...")
        # vad_filter=True 能有效过滤掉嘻哈伴奏里的幻听
        segments, _ = self.whisper.transcribe(
            audio_path,
            # 1. 简化的 Prompt：只给关键词，不给完整句子
            initial_prompt="Rap, Hip-hop, lyrics, slang.", 
            
            # 2. 提高采样门槛：如果 AI 不确定，就不要乱说话
            beam_size=5,
            best_of=5,
            
            # 3. 关键：设置静音过滤和压缩比限制
            vad_filter=True,  # 确保开启静音过滤
            vad_parameters=dict(min_silence_duration_ms=500), # 超过0.5秒没声音就跳过
            
            # 4. 幻听控制：如果这一段重复率太高，丢弃它
            compression_ratio_threshold=2.4, 
            no_speech_threshold=0.6,
            
            word_timestamps=True
        )
        
        full_data = []
        # 纯英文文本列表（用于交给翻译引擎）
        english_texts_only = []

        for segment in segments:
            clean_text = segment.text.strip()
            if clean_text:
                # 存储详细信息
                full_data.append({
                    'start': segment.start,
                    'end': segment.end,
                    'text': clean_text
                })
                # 存储纯文本
                english_texts_only.append(clean_text)
                # print(f"已提取: {clean_text}")

        # 清理临时音频
        if os.path.exists(audio_path):
            os.remove(audio_path)

        # 返回这两个列表供后续使用
        return full_data, english_texts_only

    def generate_bilingual_srt(self,full_data, english_texts, output_file="final_bilingual.srt"):
        """
        full_data: 包含 start, end, text 的列表
        english_texts: 纯英文文本列表
        """
        
        # --- 步骤 A: 构建 Prompt ---
        # 将歌词合并，并带上序号，方便模型对应
        lyrics_block = "\n".join([f"{i+1}: {text}" for i, text in enumerate(english_texts)])
        
        prompt = f"""你是一个专业的 Hip-hop 翻译官。请将以下歌词翻译成中文。
    要求：
    1. 保持行数和序号一一对应。
    2. 翻译要地道，保留 Rap 的俚语和韵味。
    3. 只返回翻译后的中文，不要包含任何解释。
    4. 语义通顺并能理解上下文含义。
    5. 根据歌词的内容设定语境。
    6. 不含脏话，敏感词汇会进行隐晦处理。

    歌词列表：
    {lyrics_block}
    """

        # --- 步骤 B: 调用本地模型翻译 ---
        print("正在调用本地 Qwen 模型进行语境翻译...")
        response = self.llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048, # 歌词长的话需要调大
            temperature=0.7
        )
        
        translated_content = response['choices'][0]['message']['content']
        
        # 解析模型返回的中文（假设模型按行返回）
        # 简单的清理逻辑：去掉序号，只留文本
        chinese_lines = []
        for line in translated_content.strip().split('\n'):
            # 去掉类似 "1: " 或 "1. " 的前缀
            clean_line = re.sub(r'^\d+[:.、\s]+', '', line)
            chinese_lines.append(clean_line)

        # --- 步骤 C: 组合生成 SRT ---
        print(f"写入双语字幕到 {output_file}...")
        with open(output_file, "w", encoding="utf-8") as f:
            for i, (item, cn_text) in enumerate(zip(full_data, chinese_lines), start=1):
                start_str = format_timestamp(item['start'])
                end_str = format_timestamp(item['end'])
                
                f.write(f"{i}\n")
                f.write(f"{start_str} --> {end_str}\n")
                f.write(f"{item['text']}\n")  # 英文原文
                f.write(f"{cn_text}\n\n")    # 中文译文

        print("✅ 所有任务已完成！")
        return output_file

    def burn_video(self, video_path, srt_path, final_path=VIDEO_OUTPUT):
        print("🎬 正在使用 FFmpeg 压制成品...")
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
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"\n✅ 压制成功！")
                print(f"🎥 成品路径: {VIDEO_OUTPUT}")
            else:
                print(f"\n❌ 压制失败！FFmpeg 报错如下:")
                print(result.stderr)
                
        except Exception as e:
            print(f"程序运行发生异常: {e}")

# ================= 运行区 =================
if __name__ == "__main__":
    app = HipHopAutoProject()
    os.makedirs("/Users/randy/Downloads/temp/", exist_ok=True)  # 确保临时目录存在
    app.temp_dir = "/Users/randy/Downloads/temp/"
    query = input("请输入歌名（如：J. Cole c l o s e）: ")
    
    # 创建一个临时目录用于存放所有过程文件
    # delete=True 会在程序结束时尝试删除，但为了稳妥，我们手动用 shutil 清理
    # base_temp_path = "/Users/randy/Downloads/"
    # if not os.path.exists(base_temp_path):
    #     os.makedirs(base_temp_path)
    # temp_dir = tempfile.mkdtemp(prefix="hiphop_tmp_",dir=base_temp_path)
    try:
        # 1. 下载：修改输出路径到临时目录
        tmp_video_path = os.path.join(app.temp_dir, "raw_video.mp4")
        # 注意：你需要稍微修改 download_step 让它接受路径参数
        vid = app.download_step(query, output_path=tmp_video_path)
        
        # 2. 识别：生成的数据在内存中
        segs, english_texts = app.transcribe_step(vid, os.path.join(app.temp_dir, "temp_audio.wav"))
        
        # 3. 翻译：字幕文件也存放在临时目录
        tmp_srt_path = os.path.join(app.temp_dir, "bilingual.srt")
        srt = app.generate_bilingual_srt(segs, english_texts, output_file=tmp_srt_path)
        
        # 4. 压制：只有最终产物保存在当前执行目录（外部）
        app.burn_video(vid, srt)
        
        print(f"\n✨ 制作完成！成品已保存至当前目录下的 {VIDEO_OUTPUT}")

    except Exception as e:
        print(f"❌ 程序运行出错: {e}")
    
    finally:
        # 无论成功还是失败，清理整个临时文件夹
        if os.path.exists(app.temp_dir):
            shutil.rmtree(app.temp_dir)
            print(f"🧹 临时文件已清理 (目录: {app.temp_dir})")