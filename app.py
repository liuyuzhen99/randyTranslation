import streamlit as st
import os
import tempfile
import shutil
from hipHopProducer import HipHopAutoProject # 导入你之前的类

st.set_page_config(page_title="Hip-Hop MV Maker", page_icon="🎵")

st.title("🎵 Hip-Hop MV 自动翻译制片机")
st.markdown("输入 YouTube 歌曲名，自动生成中英双语字幕视频。")

# 初始化后端逻辑
if 'producer' not in st.session_state:
    st.session_state.producer = HipHopAutoProject()

query = st.text_input("请输入歌曲名称：", placeholder="例如: J. Cole c l o s e")

if st.button("开始一键制作", type="primary"):
    if not query:
        st.error("请输入歌名！")
    else:
        # 创建进度条
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # 临时文件夹处理
        base_temp = os.path.expanduser("~/Downloads/")
        temp_dir = tempfile.mkdtemp(prefix="hiphop_tmp_", dir=base_temp)
        
        try:
            status_text.text("📥 步骤 1: 正在从 YouTube 下载视频...")
            tmp_video_path = os.path.join(temp_dir, "raw_video.mp4")
            vid = st.session_state.producer.download_step(query, output_path=tmp_video_path)
            progress_bar.progress(25)

            status_text.text("🎙️ 步骤 2: 正在提取音轨并识别歌词...")
            segs, english_texts = st.session_state.producer.transcribe_step(vid, os.path.join(temp_dir, "temp_audio.wav"))
            progress_bar.progress(50)

            status_text.text("🤖 步骤 3: 微调模型正在进行语境翻译...")
            tmp_srt_path = os.path.join(temp_dir, "bilingual.srt")
            srt = st.session_state.producer.generate_bilingual_srt(segs, english_texts, output_file=tmp_srt_path)
            progress_bar.progress(75)

            status_text.text("🎬 步骤 4: FFmpeg 正在压制双语字幕...")
            st.session_state.producer.burn_video(vid, srt)
            progress_bar.progress(100)

            st.success(f"✨ 制作完成！成品已保存至下载目录。")
            # 在界面上预览
            st.video("/Users/randy/Downloads/final_hiphop_mv.mp4")

        except Exception as e:
            st.error(f"发生错误: {e}")
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)