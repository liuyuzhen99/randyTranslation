import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class TaskIdFilter(logging.Filter):
    """确保每条日志都有 task_id 属性，防止 Formatter 报错"""

    def filter(self, record):
        if not hasattr(record, "task_id"):
            record.task_id = "SYSTEM"
        return True


class LogManager:
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LogManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, log_file=None):
        if LogManager._initialized:
            return

        self.log_file = self._resolve_log_file(log_file)
        self.formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - [Task:%(task_id)s] - %(message)s"
        )

        self.base_logger = logging.getLogger("hiphop_app")
        self.base_logger.setLevel(logging.INFO)

        rotating_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        rotating_handler.setFormatter(self.formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self.formatter)

        self.base_logger.addHandler(rotating_handler)
        self.base_logger.addHandler(console_handler)
        self.base_logger.addFilter(TaskIdFilter())

        LogManager._initialized = True

    @staticmethod
    def _resolve_log_file(log_file):
        configured_path = (
            log_file
            or os.getenv("LOG_FILE_PATH")
            or os.getenv("HIPHOP_APP_LOG_FILE")
        )
        if configured_path:
            log_path = Path(configured_path).expanduser()
        else:
            log_path = Path(__file__).resolve().parents[1] / "logs" / "hiphop_app.log"

        log_path.parent.mkdir(parents=True, exist_ok=True)
        return str(log_path)

    @classmethod
    def get_task_logger(cls, task_id: str):
        return logging.LoggerAdapter(logging.getLogger("hiphop_app"), {"task_id": task_id})


log_manager = LogManager()
