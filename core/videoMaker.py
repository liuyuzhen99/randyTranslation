import subprocess

def burn_video(video_path, srt_path, final_path):
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
            print(f"🎥 成品路径: {final_path}")
        else:
            print(f"\n❌ 压制失败！FFmpeg 报错如下:")
            print(result.stderr)
            
    except Exception as e:
        print(f"程序运行发生异常: {e}")