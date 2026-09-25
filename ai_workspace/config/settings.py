# 파이프라인 전역 설정
# - 환경별(dev/staging/prod) 설정 분리
# - LLM, 임베딩, 클러스터링, 크롤러 관련 파라미터 정의
# - RSS 피드 소스 목록 관리

import os
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv(override=False)


class Environment:
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


def get_environment() -> str:
    return os.getenv("ENV", Environment.DEV).lower()


class BaseSettings:

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
    # LLM 응답의 JSON 파싱이 실패했을 때 재시도할 최대 횟수 (이전에는 1000000으로
    # 사실상 무제한이었음 - 비용 폭주 위험 방지를 위해 유한한 상한으로 교체)
    MAX_JSON_PARSE_RETRIES: int = 5

    # ========== HDBSCAN ==========
    HDBSCAN_MIN_CLUSTER_SIZE: int = 3
    HDBSCAN_MIN_SAMPLES: int = 2

    # ========== Embedder ==========
    EMBEDDING_BATCH_SIZE: int = 8  # GPU 메모리 고려
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
    MAX_FETCH_RETRIES: int = 3  # RSS/본문 크롤링 네트워크 요청 최대 재시도 횟수
    
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
    LOG_LEVEL: str = "DEBUG"
    LOG_TO_FILE: bool = False
    SSL_VERIFY: bool = False  
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5


class StagingSettings(BaseSettings):
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10


class ProdSettings(BaseSettings):
    LOG_LEVEL: str = "WARNING"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 5
    DB_POOL_MAX: int = 20
    PARALLEL_WORKERS: int = 8


def get_settings() -> BaseSettings:
    env = get_environment()
    settings_map = {
        Environment.DEV: DevSettings,
        Environment.STAGING: StagingSettings,
        Environment.PROD: ProdSettings,
    }
    return settings_map.get(env, DevSettings)()


Settings = get_settings()
