"""
Logging configuration for AI Workspace
Provides consistent logging across all modules
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# Log directory
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


class ColoredFormatter(logging.Formatter):
    """Colored output formatter for console"""

    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    console: bool = True
) -> logging.Logger:
    """
    Setup logging configuration for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional log file name (will be created in logs/ directory)
        console: Whether to output to console

    Returns:
        Root logger instance
    """
    # Get environment-based log level
    env_level = os.getenv("LOG_LEVEL", level).upper()
    log_level = getattr(logging, env_level, logging.INFO)

    # Root logger configuration
    root_logger = logging.getLogger("ai_workspace")
    root_logger.setLevel(log_level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Log format
    log_format = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(ColoredFormatter(log_format, datefmt=date_format))
        root_logger.addHandler(console_handler)

    # File handler
    if log_file:
        file_path = LOG_DIR / log_file
        file_handler = logging.FileHandler(file_path, encoding='utf-8')
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        root_logger.addHandler(file_handler)

    # Auto log file based on date
    if os.getenv("LOG_TO_FILE", "false").lower() == "true":
        daily_log = LOG_DIR / f"ai_workspace_{datetime.now().strftime('%Y%m%d')}.log"
        daily_handler = logging.FileHandler(daily_log, encoding='utf-8')
        daily_handler.setLevel(log_level)
        daily_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        root_logger.addHandler(daily_handler)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.

    Args:
        name: Module name (e.g., 'crawler.rss_collector')

    Returns:
        Logger instance
    """
    return logging.getLogger(f"ai_workspace.{name}")


# Initialize default logging on import
_root_logger = setup_logging()
