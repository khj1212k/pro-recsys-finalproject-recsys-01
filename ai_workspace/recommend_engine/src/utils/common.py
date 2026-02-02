# src/utils/common.py
"""공통 유틸리티 함수"""

import os
import yaml
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional


def get_project_root() -> str:
    """프로젝트 루트 디렉토리 반환"""
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


def get_data_path(filename: str, config: Dict = None) -> str:
    """데이터 파일의 전체 경로 반환"""
    if config is None:
        config = load_config()
    return os.path.join(get_project_root(), config['data']['base_path'], filename)


def setup_logging(
    log_dir: str = None,
    level: str = "INFO",
    log_to_file: bool = True,
    log_to_console: bool = True,
    name: str = "recommend_engine"
) -> logging.Logger:
    """로깅 설정"""
    if log_dir is None:
        log_dir = os.path.join(get_project_root(), "logs")
    elif not os.path.isabs(log_dir):
        log_dir = os.path.join(get_project_root(), log_dir)
    
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    logger.handlers.clear()
    
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    if log_to_file:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"train_{timestamp}.log")
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def save_json(data: Any, filepath: str, indent: int = 2):
    """JSON 파일 저장"""
    if not os.path.isabs(filepath):
        filepath = os.path.join(get_project_root(), filepath)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def load_json(filepath: str) -> Any:
    """JSON 파일 로딩"""
    if not os.path.isabs(filepath):
        filepath = os.path.join(get_project_root(), filepath)
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def ensure_dir(path: str) -> str:
    """디렉토리 생성 후 경로 반환"""
    if not os.path.isabs(path):
        path = os.path.join(get_project_root(), path)
    os.makedirs(path, exist_ok=True)
    return path
