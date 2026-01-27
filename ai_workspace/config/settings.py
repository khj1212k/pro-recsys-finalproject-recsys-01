"""
Configuration settings for ai_workspace
"""
import os
from dotenv import load_dotenv

load_dotenv(override=True)


class Settings:
    # ========== OpenAI ==========
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    # ========== LangGraph ==========
    MAX_RETRY_CLUSTER_EVAL = 2  # Max retries for cluster evaluation
    MAX_RETRY_NEWSLETTER_EVAL = 3  # Max retries for newsletter evaluation
    
    # ========== HDBSCAN ==========
    HDBSCAN_MIN_CLUSTER_SIZE = 3
    HDBSCAN_MIN_SAMPLES = 2
    
    # ========== Embedder ==========
    EMBEDDING_BATCH_SIZE = 30
    EMBEDDING_DIM = 1024
    
    # ========== Pipeline ==========
    DEFAULT_CLUSTER_LIMIT = None  # None means process all clusters
    
    # ========== Quality Thresholds ==========
    MIN_NEWSLETTER_SCORE = 7  # Minimum score to pass newsletter evaluation (0-10)
    MIN_CLUSTER_CONFIDENCE = 0.7  # Minimum confidence to pass cluster evaluation

    # ========== Crawler Settings ==========
    PARALLEL_WORKERS = 4
    REQUEST_TIMEOUT = 15
    SELENIUM_PAGE_LOAD_TIMEOUT = 60
    
    # ========== RSS Feeds ==========
    # Format: 'source_name': ('strategy', 'url')
    # strategy: 'direct' = trafilatura, 'selenium' = selenium + trafilatura
    RSS_FEEDS = {
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
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
