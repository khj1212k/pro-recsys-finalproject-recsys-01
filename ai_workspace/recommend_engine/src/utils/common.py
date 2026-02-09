# src/utils/common.py
"""
공통 유틸리티 함수 및 로거 관리
(logger.py의 기능을 통합하여 순환 참조 방지)
"""

import os
import sys
import yaml

import logging
from datetime import datetime
from typing import Dict, Any, Optional

# =============================================================================
# Path & Config Utils
# =============================================================================

def get_project_root() -> str:
    """프로젝트 루트 디렉토리 반환"""
    # 현재 파일(src/utils/common.py)의 상위(utils)의 상위(src)의 상위(root)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(current_dir))


def load_config(config_path: str = None) -> Dict[str, Any]:
    """YAML 설정 파일 로딩"""
    if config_path is None:
        config_path = os.path.join(get_project_root(), "config", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(get_project_root(), config_path)
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def ensure_dir(path: str) -> str:
    """디렉토리 생성 후 경로 반환"""
    if not os.path.isabs(path):
        path = os.path.join(get_project_root(), path)
    os.makedirs(path, exist_ok=True)
    return path


# =============================================================================
# Logging Utils (Merged from logger.py)
# =============================================================================

def get_logger(name: str = "RecSys", log_dir: str = "logs") -> logging.Logger:
    """
    로거 생성 및 반환 (Singleton 패턴 유사 적용)
    
    Args:
        name: 로거 이름 (예: 'DataLoader', 'MainExecutor')
        log_dir: 로그 파일 저장 경로 (프로젝트 루트 기준)
    """
    # 1. 로거 가져오기
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # 2. 중복 핸들러 방지 (이미 핸들러가 있으면 그대로 반환)
    if logger.handlers:
        return logger
    
    # 3. 포맷 설정
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 4. 콘솔 핸들러 (StdOut)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 5. 파일 핸들러 (날짜별 저장)
    try:
        root_path = get_project_root()
        abs_log_dir = os.path.join(root_path, log_dir)
        os.makedirs(abs_log_dir, exist_ok=True)
        
        today = datetime.now().strftime("%Y%m%d")
        log_filename = f"recsys_{today}.log"
        file_path = os.path.join(abs_log_dir, log_filename)
        
        file_handler = logging.FileHandler(file_path, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    except Exception as e:
        print(f"⚠️ 로그 파일 핸들러 설정 실패: {e}")
    
    # 부모 로거 전파 방지 (중복 출력 방지)
    logger.propagate = False
    
    return logger