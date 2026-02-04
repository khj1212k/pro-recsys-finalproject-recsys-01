# scripts/generate_embeddings.py
import os
import sys
import pickle
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from tqdm import tqdm

# 1. 프로젝트 루트 경로 설정
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.common import load_config
from src.utils.embedder import BGEEmbedder 

# DB에서 뉴스레터 로드 후 임베딩 생성 및 DB에 임베딩 업로드
def generate_embeddings_from_db():
    print("🚀 뉴스레터 임베딩 생성 및 DB 업로드 스크립트 시작")
    
    # 1. DB 연결
    config = load_config()
    db_conf = config['database']
    url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
    engine = create_engine(url)
    
    # 2. 데이터 조회
    print("🔌 DB에서 뉴스 데이터 조회 중...")
    try:
        # 이미 임베딩이 있는 데이터는 건너뛰고 싶다면 WHERE 조건을 추가할 수도 있음
        query = "SELECT news_letter_id, news_letter_title, news_letter_content, news_letter_category FROM news_letter"
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
    except Exception:
        query = "SELECT news_letter_id, news_letter_title, news_letter_content FROM news_letter"
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
        df['news_letter_category'] = ""

    print(f"📊 총 {len(df)}개의 데이터 로드 완료")

    # 3. 도구(Embedder) 초기화
    embedder = BGEEmbedder(verbose=True)
    
    # 4. 텍스트 합치기
    texts = []
    ids = []
    for _, row in df.iterrows():
        parts = [f"제목: {row['news_letter_title']}"]
        if row['news_letter_category']:
            parts.append(f"카테고리: {row['news_letter_category']}")
        parts.append(f"내용: {row['news_letter_content']}")
        texts.append("\n".join(parts))
        ids.append(row['news_letter_id'])
    
    # 5. 임베딩 생성 (Batch)
    print("🧠 임베딩 생성 중... (GPU/CPU)")
    embeddings, elapsed = embedder.encode_batch(texts, batch_size=32)
    
    # 6. DB 업데이트 (핵심 로직 추가)
    print("💾 생성된 임베딩을 DB에 업로드 중...")
    
    update_query = text("""
        UPDATE news_letter 
        SET news_letter_embedding = :emb 
        WHERE news_letter_id = :nid
    """)
    
    success_count = 0
    
    # SQLAlchemy Connection으로 업데이트 수행
    with engine.begin() as conn:
        for i, emb in enumerate(tqdm(embeddings, desc="Updating DB")):
            if emb is not None:
                # pgvector 포맷인 문자열 "[0.1, 0.2, ...]" 형태로 변환
                # (np.array -> list -> str)
                emb_str = str(emb) 
                
                conn.execute(update_query, {"emb": emb_str, "nid": ids[i]})
                success_count += 1
                
    print(f"✅ DB 업데이트 완료: 총 {success_count}건 반영됨")

if __name__ == "__main__":
    generate_embeddings_from_db()