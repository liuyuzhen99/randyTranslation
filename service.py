import os
import uuid
import shutil
import threading
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Dict
import traceback

# 导入你原来的类 (假设你的代码文件名是 hiphop_logic.py)
from hipHopProducer import HipHopAutoProject 
# 为了演示，我假设它就在当前文件中或已正确导入

from logger_manager import LogManager

# 获取一个系统的全局 logger
system_logger = LogManager.get_task_logger("SYSTEM")

app = FastAPI(title="Hip-hop MV 自动化工坊 API")

# --- 全局状态管理 ---
# 用于存储任务状态：{"task_id": {"status": "processing", "progress": "正在下载...", "result": None}}
task_status: Dict[str, dict] = {}
# 初始化你的项目实例 (加载模型)
producer = HipHopAutoProject()

# --- 请求/响应模型 ---
class TaskRequest(BaseModel):
    song_name: str

class TaskResponse(BaseModel):
    task_id: str
    message: str

# --- 核心逻辑封装 ---
def run_production_pipeline(task_id: str, song_name: str):
    # 只需要这一行，后续所有的 info/error 都会自动带上 [Task:xxxx]
    logger = LogManager.get_task_logger(task_id)
    
    logger.info(f"开始处理任务，歌名: {song_name}, 任务ID: {task_id}")
    """真正的耗时任务流水线"""
    # 为每个任务创建独立的临时子目录，避免冲突
    task_temp_dir = os.path.join("/Users/randy/Downloads/temp/", task_id)
    os.makedirs(task_temp_dir, exist_ok=True)
    producer.temp_dir = task_temp_dir  # 确保生产类使用这个临时目录
    
    try:
        task_status[task_id]["status"] = "processing"
        
        # 1. 下载
        logger.info(f"[Task:{task_id}] 📥 正在下载视频...")
        task_status[task_id]["progress"] = "📥 正在搜索并下载视频..."
        tmp_video_path = os.path.join(task_temp_dir, "raw_video.mp4")
        vid = producer.download_step(song_name, output_path=tmp_video_path)
        
        # 2. 识别与人声分离
        logger.info(f"[Task:{task_id}] 🎙️ 正在提取音轨...")
        task_status[task_id]["progress"] = "🎙️ 正在分离人声并识别歌词 (耗时较长)..."
        audio_tmp = os.path.join(task_temp_dir, "temp_audio.wav")
        segs, english_texts = producer.transcribe_step(vid, audio_tmp)
        
        # 3. 翻译
        logger.info(f"[Task:{task_id}] 🤖 正在调用 Qwen 进行微调模型翻译...")
        task_status[task_id]["progress"] = "🤖 正在调用 Qwen 进行微调模型翻译..."
        tmp_srt_path = os.path.join(task_temp_dir, "bilingual.srt")
        srt = producer.generate_bilingual_srt(segs, english_texts, output_file=tmp_srt_path)
        
        # 4. 压制
        # 注意：这里 VIDEO_OUTPUT 可以根据 task_id 动态生成，防止覆盖
        logger.info(f"[Task:{task_id}] 🎬 正在合成双语字幕视频...")
        final_output = f"/Users/randy/Downloads/MV_{task_id}.mp4"
        task_status[task_id]["progress"] = "🎬 正在合成双语字幕视频..."
        
        # 暂时借用 producer 的 burn_video，但需要让它支持自定义输出路径
        # 建议修改原类方法接受 output_path 参数
        producer.burn_video(vid, srt, final_path=final_output) 
        
        task_status[task_id]["status"] = "completed"
        task_status[task_id]["progress"] = "✨ 制作完成！"
        task_status[task_id]["result"] = final_output

    except Exception as e:
        # error_detail = traceback.format_exc() 
        # print(f"‼️ 任务 {task_id} 崩溃了！详情如下：\n{error_detail}")
        task_status[task_id]["status"] = "failed"
        task_status[task_id]["progress"] = f"❌ 错误: {str(e)}"
        logger.error(f"[Task:{task_id}] ‼️ 任务失败: {str(e)}", exc_info=True)
    
    finally:
        # 清理该任务的临时文件夹
        if os.path.exists(task_temp_dir):
            shutil.rmtree(task_temp_dir)
        logger.info(f"[Task:{task_id}] 🏁 后台流水线执行完毕")

# --- API 路由 ---

@app.post("/create_task", response_model=TaskResponse)
async def create_task(request: TaskRequest, background_tasks: BackgroundTasks):
    # 1. 记录请求进入
    system_logger.info(f"收到创建任务请求: 歌名={request.song_name}")
    # 打印请求头中的 User-Agent，看看是不是同一个东西发来的
    import time
    print(f"🔔 收到请求时间: {time.time()}, 歌名: {request.song_name}")
    """用户提交歌名，立即返回任务ID"""
    task_id = str(uuid.uuid4())[:8] # 取简短ID
    task_status[task_id] = {
        "status": "pending",
        "progress": "已加入队列",
        "result": None,
        "song_name": request.song_name
    }
    
    # 将耗时任务放入后台执行
    background_tasks.add_task(run_production_pipeline, task_id, request.song_name)
    system_logger.info(f"任务 {task_id} 已创建并加入后台队列，歌名: {request.song_name}")
    
    return {"task_id": task_id, "message": "任务已启动，请稍后通过 ID 查询进度"}

@app.get("/check_status/{task_id}")
async def check_status(task_id: str):
    system_logger.info(f"查询任务状态: {task_id}")
    """前端轮询此接口获取实时进度"""
    if task_id not in task_status:
        system_logger.warning(f"查询了不存在的任务ID: {task_id}")
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_status[task_id]

@app.get("/list_tasks")
async def list_tasks():
    system_logger.info("查询所有任务状态")
    """查看当前所有任务状态"""
    return task_status

if __name__ == "__main__":
    import uvicorn
    # 启动服务器，外网或局域网访问记得修改 host
    uvicorn.run(app, host="127.0.0.1", port=8000)