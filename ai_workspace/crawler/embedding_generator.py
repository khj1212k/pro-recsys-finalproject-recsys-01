# 기사/뉴스레터 임베딩 배치 생성 모듈
from typing import List, Tuple, Any
import logging
from tqdm import tqdm

from db.connection import get_connection, release_connection
from core.embedder import NewsEmbedder
from config.settings import Settings
from utils.logger import setup_logger

logger = setup_logger(__name__, logging.INFO)

# Import helpers for handling serialization
from db.batch_manager import convert_numpy


def generate_embeddings_for_articles(batch_size: int = None, force_cpu: bool = False) -> int:
    if batch_size is None:
        batch_size = Settings.EMBEDDING_BATCH_SIZE
    
    # 1. 대상 기사 조회
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT raw_news_id, raw_news_title, raw_news_content
            FROM news_raw
            WHERE raw_news_content IS NOT NULL 
              AND raw_news_content != ''
              AND embedding_result IS NULL
              AND (news_letter_id IS NULL OR news_letter_id != -1)
            ORDER BY raw_news_id
        """)
        articles = cur.fetchall()
    finally:
        release_connection(conn)
    
    if not articles:
        logger.info("💤 No articles to embed.")
        return 0
    
    logger.info(f"📐 Starting Article Embeddings (Count: {len(articles)}, Batch: {batch_size})")
    
    success_count = 0
    
    # 2. 배치 처리
    with NewsEmbedder(force_cpu=force_cpu, verbose=True) as embedder:
        with tqdm(total=len(articles), desc="Processing Articles") as pbar:
            for i in range(0, len(articles), batch_size):
                batch = articles[i : i + batch_size]
                
                # 텍스트 준비 (제목 + \n\n + 본문)
                texts = [f"{art[1]}\n\n{art[2]}" for art in batch]
                ids = [art[0] for art in batch]
                
                # 임베딩 생성
                try:
                    embeddings, _ = embedder.generate_embeddings_batch(texts, batch_size=batch_size)
                    
                    # DB 업데이트
                    _update_embeddings_batch(ids, embeddings, is_newsletter=False)
                    success_count += len(embeddings)
                    pbar.update(len(batch))
                    
                except Exception as e:
                    logger.error(f"❌ Batch failed: {e}")
                    continue
    
    logger.info(f"✨ Article Embeddings Complete! ({success_count} success)")
    return success_count


def generate_embeddings_for_newsletters(batch_size: int = None, force_cpu: bool = False) -> int:
    if batch_size is None:
        batch_size = Settings.EMBEDDING_BATCH_SIZE
        
    # 1. 대상 뉴스레터 조회
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT news_letter_id, news_letter_title, news_letter_sentence, news_letter_content
            FROM news_letter
            WHERE news_letter_embedding IS NULL
            ORDER BY news_letter_id
        """)
        newsletters = cur.fetchall()
    finally:
        release_connection(conn)
    
    if not newsletters:
        logger.info("💤 No newsletters to embed.")
        return 0
        
    logger.info(f"📐 Starting Newsletter Embeddings (Count: {len(newsletters)})")
    
    success_count = 0
    
    # 2. 배치 처리
    with NewsEmbedder(force_cpu=force_cpu, verbose=True, l2_normalize=True) as embedder:
        with tqdm(total=len(newsletters), desc="Processing Newsletters") as pbar:
            for i in range(0, len(newsletters), batch_size):
                batch = newsletters[i : i + batch_size]
                
                # 텍스트 준비: 제목 + 문장 + 본문
                texts = []
                ids = []
                for nl in batch:
                    title = nl[1] or ""
                    sentence = nl[2] or ""
                    content = nl[3] or ""
                    texts.append(f"{title} {sentence} {content}")
                    ids.append(nl[0])
                    
                try:
                    embeddings, _ = embedder.generate_embeddings_batch(texts, batch_size=batch_size)
                    
                    _update_embeddings_batch(ids, embeddings, is_newsletter=True)
                    success_count += len(embeddings)
                    pbar.update(len(batch))
                    
                except Exception as e:
                    logger.error(f"❌ Batch failed: {e}")
                    continue
    
    logger.info(f"✨ Newsletter Embeddings Complete! ({success_count} success)")
    return success_count


def _update_embeddings_batch(ids: List[int], embeddings: List[Any], is_newsletter: bool):
    if not embeddings:
        return

    table = "news_letter" if is_newsletter else "news_raw"
    col_id = "news_letter_id" if is_newsletter else "raw_news_id"
    col_emb = "news_letter_embedding" if is_newsletter else "embedding_result"
    
    conn = get_connection()
    try:
        cur = conn.cursor()
        for record_id, embedding in zip(ids, embeddings):
            if embedding:
                embedding_str = str(embedding)
                cur.execute(f"""
                    UPDATE {table}
                    SET {col_emb} = %s
                    WHERE {col_id} = %s
                """, (embedding_str, record_id))
        conn.commit()
    finally:
        release_connection(conn)
