"""
ai_workspace를 위한 환경 설정
환경 기반 설정 지원 (dev/staging/prod)
"""
import os
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv(override=False)


class Environment:
    """환경 상수 정의"""
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


def get_environment() -> str:
    """ENV 환경변수로부터 현재 환경을 조회합니다"""
    return os.getenv("ENV", Environment.DEV).lower()


class BaseSettings:
    """모든 환경에서 공유되는 기본 설정"""

    # ========== LLM Provider ==========
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "naver")  # 'naver' or 'openai'

    # ========== Naver HyperCLOVA X ==========
    NCP_CLOVASTUDIO_API_KEY: str = os.getenv("NCP_CLOVASTUDIO_API_KEY", "")
    NCP_APIGW_API_KEY: str = os.getenv("NCP_APIGW_API_KEY", "")
    HYPERCLOVA_MODEL: str = os.getenv("HYPERCLOVA_MODEL", "HCX-003")

    # ========== OpenAI (Fallback) ==========
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # ========== LangGraph ==========
    MAX_RETRY_CLUSTER_EVAL: int = 2
    MAX_RETRY_NEWSLETTER_EVAL: int = 3

    # ========== HDBSCAN ==========
    HDBSCAN_MIN_CLUSTER_SIZE: int = 3
    HDBSCAN_MIN_SAMPLES: int = 2

    # ========== Embedder ==========
    EMBEDDING_BATCH_SIZE: int = 16  # GPU 메모리 고려
    EMBEDDING_DIM: int = 1024

    # ========== Pipeline ==========
    DEFAULT_CLUSTER_LIMIT: int = None

    # ========== Quality Thresholds ==========
    MIN_NEWSLETTER_SCORE: int = 7
    MIN_CLUSTER_CONFIDENCE: float = 0.7
    
    # ========== Pipeline Stages ==========
    STAGE_NAMES: Dict[int, str] = {
        0: "User Embedding",
        1: "RSS Collection",
        2: "Content Extraction",
        3: "Article Embedding",
        4: "Clustering",
        5: "Newsletter Generation",
        6: "Newsletter Embedding",
    }
    
    # ========== Retry Configuration ==========
    RETRY_EXPONENTIAL_BASE: float = 2.0
    MAX_RETRY_WAIT_SECONDS: int = 64
    
    # ========== Crawler Settings ==========
    PARALLEL_WORKERS: int = 8  # 병렬 크롤링 워커
    REQUEST_TIMEOUT: int = 15
    SELENIUM_PAGE_LOAD_TIMEOUT: int = 60

    # ========== SSL Verification ==========
    SSL_VERIFY: bool = True

    # ========== Logging ==========
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = False

    # ========== Database Pool ==========
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ========== RSS Feeds ==========
    RSS_FEEDS: Dict[str, Tuple[str, str]] = {
        # 종합 일간지
        '동아일보': ('direct', 'https://rss.donga.com/total.xml'),
        '경향신문': ('direct', 'https://www.khan.co.kr/rss/rssdata/total_news.xml'),
        '매일경제': ('direct', 'https://www.mk.co.kr/rss/30000001/'),
        '한국경제': ('direct', 'https://www.hankyung.com/feed/all-news'),
        '국민일보': ('direct', 'https://www.kmib.co.kr/rss/data/kmibRssAll.xml'),
        '세계일보': ('direct', 'https://www.segye.com/Articles/RSSList/segye_recent.xml'),

        # 과학/기술
        '전자신문_IT': ('direct', 'http://rss.etnews.com/03.xml'),
        '전자신문_AI': ('direct', 'http://rss.etnews.com/04046.xml'),
        '전자신문_과학': ('direct', 'http://rss.etnews.com/20.xml'),
        'AI타임스': ('direct', 'https://www.aitimes.com/rss/allArticle.xml'),
    }

    # ========== User Agent ==========
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


class DevSettings(BaseSettings):
    """지정된 개발 환경 설정"""
    LOG_LEVEL: str = "DEBUG"
    LOG_TO_FILE: bool = False
    SSL_VERIFY: bool = False  # Disable SSL verification in dev
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5


class StagingSettings(BaseSettings):
    """Staging environment settings"""
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10


class ProdSettings(BaseSettings):
    """운영(Production) 환경 설정"""
    LOG_LEVEL: str = "WARNING"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 5
    DB_POOL_MAX: int = 20
    PARALLEL_WORKERS: int = 8


def get_settings() -> BaseSettings:
    """현재 환경에 맞는 설정 객체를 반환합니다"""
    env = get_environment()
    settings_map = {
        Environment.DEV: DevSettings,
        Environment.STAGING: StagingSettings,
        Environment.PROD: ProdSettings,
    }
    return settings_map.get(env, DevSettings)()


# Default Settings instance (for backward compatibility)
Settings = get_settings()
