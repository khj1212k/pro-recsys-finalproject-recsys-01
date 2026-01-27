"""
임베딩 생성기
news_raw 테이블의 기사들에 대해 BGE-M3 임베딩 생성
"""
from db.connection import get_connection
from core.embedder import NewsEmbedder
from config.settings import Settings
from tqdm import tqdm


def generate_embeddings_for_articles(batch_size: int = None, force_cpu: bool = False):
    """
    본문이 있고 임베딩이 없는 기사들에 대해 임베딩 생성
    
    Args:
        batch_size: 배치 크기 (기본: Settings.EMBEDDING_BATCH_SIZE)
        force_cpu: CPU 강제 사용
    """
    if batch_size is None:
        batch_size = Settings.EMBEDDING_BATCH_SIZE
    
    # 1. 대상 조회
    conn = get_connection()
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
    conn.close()
    
    if not articles:
        print("💤 임베딩 생성할 기사가 없습니다.")
        return 0
    
    print(f"📐 임베딩 생성 시작! (대상: {len(articles)}건, 배치: {batch_size})")
    
    # 2. 임베더 초기화
    embedder = NewsEmbedder(force_cpu=force_cpu, verbose=True)
    
    # 3. 배치 처리
    success_count = 0
    
    for i in tqdm(range(0, len(articles), batch_size), desc="🔢 임베딩 생성 중"):
        batch = articles[i:i+batch_size]
        
        # 텍스트 준비 (제목 + 본문)
        texts = [f"{art[1]}\n\n{art[2]}" for art in batch]
        ids = [art[0] for art in batch]
        
        # 임베딩 생성
        embeddings, _ = embedder.generate_embeddings_batch(texts, batch_size=batch_size)
        
        # DB 저장
        conn = get_connection()
        cur = conn.cursor()
        
        for raw_news_id, embedding in zip(ids, embeddings):
            if embedding:
                # PostgreSQL pgvector 형식으로 저장
                embedding_str = str(embedding)
                cur.execute("""
                    UPDATE news_raw
                    SET embedding_result = %s, updated_at = NOW()
                    WHERE raw_news_id = %s
                """, (embedding_str, raw_news_id))
                success_count += 1
        
        conn.commit()
        conn.close()
    
    print(f"✨ 임베딩 생성 완료! {success_count}건 성공")
    return success_count


if __name__ == "__main__":
    generate_embeddings_for_articles()
