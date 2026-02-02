# src/utils/logger.py
import logging
import os
import sys
from datetime import datetime
from .common import get_project_root

def setup_logger(name: str = "RecSys", log_dir: str = "logs"):
    """
    콘솔 및 파일 로깅 설정
    """
    # 로그 디렉토리 생성
    root_path = get_project_root()
    log_path = os.path.join(root_path, log_dir)
    os.makedirs(log_path, exist_ok=True)
    
    # 로거 생성
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # 중복 핸들러 방지
    if logger.handlers:
        return logger
    
    # 포맷 설정
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. 콘솔 핸들러 (화면 출력)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 2. 파일 핸들러 (날짜별 저장)
    today = datetime.now().strftime("%Y%m%d")
    file_handler = logging.FileHandler(
        os.path.join(log_path, f"recsys_{today}.log"), 
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger