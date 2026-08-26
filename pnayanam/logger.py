import logging
from logging.handlers import TimedRotatingFileHandler
import configparser
import os


class Logger:

    def __init__(self, name):

        config = configparser.ConfigParser()
        config.read("/var/pnayanam/pnayanam.ini")

        level_name = config.get(
            "logger",
            "level",
            fallback="INFO"
        ).upper()

        level = getattr(logging, level_name, logging.INFO)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)

        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        file_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, "pnayanam.log"),
            when="D",
            interval=1,
            backupCount=7,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)

        if not self.logger.handlers:
            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

    def info(self, msg):
        self.logger.info(msg)

    def debug(self, msg):
        self.logger.debug(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)

    def exception(self, msg):
        self.logger.exception(msg)