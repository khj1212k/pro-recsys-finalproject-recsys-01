# scripts/generate_embeddings.py
import os
import sys
import pickle
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from tqdm import tqdm

# 1. 프로젝트 루트 경로를 잡아줍니다. (src를 import하기 위해)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.common import load_config
# [핵심] 원래 자리에 있는 도구를 가져옵니다.
from src.utils.embedder import BGEEmbedder 

def generate_embeddings_from_db():
    print("🚀 뉴스레터 임베딩 생성 스크립트 시작")
    
    # 1. DB 연결
    config = load_config()
    db_conf = config['database']
    url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
    engine = create_engine(url)
    
    # 2. 데이터 조회
    print("🔌 DB에서 뉴스 데이터 조회 중...")
    try:
        query = "SELECT news_letter_id, news_letter_title, news_letter_content, news_letter_category FROM news_letter"
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
    except Exception:
        # 카테고리가 없는 경우 대비
        query = "SELECT news_letter_id, news_letter_title, news_letter_content FROM news_letter"
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
        df['news_letter_category'] = ""

    print(f"📊 총 {len(df)}개의 데이터 로드 완료")

    # 3. 도구(Embedder) 초기화
    embedder = BGEEmbedder(verbose=True)
    
    # 4. 텍스트 합치기 (제목 + 카테고리 + 본문)
    texts = []
    for _, row in df.iterrows():
        # embedder.py에 있는 encode_news 함수 로직을 흉내내거나 직접 써도 됨
        parts = [f"제목: {row['news_letter_title']}"]
        if row['news_letter_category']:
            parts.append(f"카테고리: {row['news_letter_category']}")
        parts.append(f"내용: {row['news_letter_content']}")
        texts.append("\n".join(parts))
    
    # 5. 임베딩 생성 (Batch)
    print("🧠 임베딩 생성 중...")
    embeddings, elapsed = embedder.encode_batch(texts, batch_size=16)
    
    # 6. 저장
    embedded_data = {}
    for idx, row in tqdm(df.iterrows(), total=len(df), desc="Saving"):
        nid = int(row['news_letter_id'])
        if embeddings[idx] is not None:
            embedded_data[nid] = {
                'embedding': np.array(embeddings[idx], dtype=np.float32),
                'title': row['news_letter_title']
            }
            
    base_path = config.get('data', {}).get('base_path', 'data')
    output_path = os.path.join(base_path, 'embedded_news.pkl')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'wb') as f:
        pickle.dump(embedded_data, f)
        
    print(f"💾 저장 완료: {output_path} ({elapsed:.2f}초 소요)")

if __name__ == "__main__":
    generate_embeddings_from_db()