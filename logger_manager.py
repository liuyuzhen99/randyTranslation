import logging
import os
import traceback

class TaskIdFilter(logging.Filter):
    """确保每条日志都有 task_id 属性，防止 Formatter 报错"""
    def filter(self, record):
        if not hasattr(record, 'task_id'):
            record.task_id = "SYSTEM"
        return True

class LogManager:
    _instance = None  # 用于实现单例

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LogManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_file="/Users/randy/Downloads/temp/hiphop_app.log"):
        if self._initialized: return # 确保只配置一次 Handler
        
        self.log_file = log_file
        self.formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - [Task:%(task_id)s] - %(message)s"
        )
        
        # 这里的 logger 是持久存在的单例
        self.base_logger = logging.getLogger("hiphop_app")
        self.base_logger.setLevel(logging.INFO)
        
        # 只在第一次启动时挂载 Handler
        fh = logging.FileHandler(self.log_file)
        fh.setFormatter(self.formatter)
        self.base_logger.addHandler(fh)
        self.base_logger.addFilter(TaskIdFilter())
        
        self._initialized = True

    @classmethod
    def get_task_logger(cls, task_id: str):
        # 每次调用只是创建一个轻量级的包装壳，随用随弃
        return logging.LoggerAdapter(logging.getLogger("hiphop_app"), {"task_id": task_id})

# 实例化单例
log_manager = LogManager()