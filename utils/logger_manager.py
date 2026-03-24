import logging
from logging.handlers import RotatingFileHandler
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
    _initialized = False  # 确保 Handler 只配置一次

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LogManager, cls).__new__(cls)
            # cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_file="/Users/randy/Downloads/temp/hiphop_app.log"):
        if LogManager._initialized: return # 确保只配置一次 Handler
        
        self.log_file = log_file
        self.formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - [Task:%(task_id)s] - %(message)s"
        )
        
        # 这里的 logger 是持久存在的单例
        self.base_logger = logging.getLogger("hiphop_app")
        self.base_logger.setLevel(logging.INFO)

        # --- 核心修改：引入自动切割处理器 ---
        # maxBytes: 10 * 1024 * 1024 = 10MB
        # backupCount: 5 (保留最近的 5 个备份文件：.log.1, .log.2, ...)
        rotating_handler = RotatingFileHandler(
            self.log_file, 
            maxBytes=10 * 1024 * 1024, 
            backupCount=5,
            encoding="utf-8"
        )
        rotating_handler.setFormatter(self.formatter)

        # 控制台输出（可选，便于调试）
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self.formatter)
        
        # 添加处理器和过滤器
        self.base_logger.addHandler(rotating_handler)
        self.base_logger.addHandler(console_handler)
        self.base_logger.addFilter(TaskIdFilter())
        
        # 只在第一次启动时挂载 Handler
        # fh = logging.FileHandler(self.log_file)
        # fh.setFormatter(self.formatter)
        # self.base_logger.addHandler(fh)
        # self.base_logger.addFilter(TaskIdFilter())
        
        self._initialized = True

    @classmethod
    def get_task_logger(cls, task_id: str):
        # 每次调用只是创建一个轻量级的包装壳，随用随弃
        return logging.LoggerAdapter(logging.getLogger("hiphop_app"), {"task_id": task_id})

# 实例化单例
log_manager = LogManager()