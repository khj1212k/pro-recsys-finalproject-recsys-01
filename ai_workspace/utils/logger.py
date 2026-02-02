"""
AI Workspace를 위한 표준 로깅 유틸리티
"""
import logging
import sys

def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    일관된 포맷을 갖춘 표준 로거를 설정합니다.
    
    Args:
        name: 로거 이름
        level: 로깅 레벨
        
    Returns:
        설정된 로거 인스턴스
    """
    logger = logging.getLogger(name)
    
    # 중복 핸들러 추가 방지
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout) # 표준 출력(터미널)으로 로그를 보냄
        
        # 일관된 포맷: 타임스탬프 | 레벨 | 메시지
        # 깔끔한 출력을 위해 메시지에서 로거 이름은 생략 ( %(name)s ) 
        formatter = logging.Formatter(
                                fmt='%(asctime)s | %(levelname)s | %(message)s',
                                datefmt='%H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    
    logger.setLevel(level)
    return logger
