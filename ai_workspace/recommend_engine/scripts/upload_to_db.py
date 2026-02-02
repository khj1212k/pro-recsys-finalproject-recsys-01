# scripts/upload_to_db.py
import os
import sys
import json
import argparse
from sqlalchemy import create_engine, text
from tqdm import tqdm

# 프로젝트 루트 경로 추가 (모듈 import용)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils.common import load_config

def upload_to_db(json_file_path: str, table_name: str = "User_Preferred_Newsletter"):
    """
    JSON 추천 결과를 DB에 업로드
    """
    # 1. 설정 로드 & DB 연결
    config = load_config()
    db_conf = config['database']
    url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
    engine = create_engine(url)
    
    print(f"🔌 DB 연결: {db_conf['dbname']}")
    
    # 2. JSON 파일 로드
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"📂 파일 로드: {json_file_path} (총 {len(data)}명 유저)")

    # 3. DB 업로드 트랜잭션
    inserted_count = 0
    
    with engine.begin() as conn: # 트랜잭션 시작
        for user_rec in tqdm(data, desc="DB Uploading"):
            user_id = user_rec['user_id']
            
            # JSON 키 변경 반영: news_ids -> news_letter_ids
            # 만약 news_letter_ids가 없으면 news_ids를 찾도록(호환성) get 사용
            news_ids = user_rec.get('news_letter_ids', user_rec.get('news_ids', []))
            
            if not news_ids:
                print(f"⚠️ 경고: User {user_id}의 추천 목록이 비어있습니다.")
                continue

            # (옵션) 기존 데이터 삭제가 필요하면 아래 주석 해제
            # conn.execute(text(f'DELETE FROM "{table_name}" WHERE user_id = :uid'), {"uid": user_id})
            
            # 데이터 삽입
            for nid in news_ids:
                query = text(f"""
                    INSERT INTO "{table_name}" (user_id, news_letter_id) 
                    VALUES (:uid, :nid)
                    ON CONFLICT DO NOTHING -- 중복 방지
                """)
                conn.execute(query, {"uid": user_id, "nid": nid})
                inserted_count += 1
                
    print(f"✅ 업로드 완료! 총 {inserted_count}건의 추천 데이터가 '{table_name}' 테이블에 저장되었습니다.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, required=True, help='업로드할 JSON 파일 경로')
    parser.add_argument('--table', type=str, default='User_Preferred_Newsletter', help='대상 테이블 이름')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.file):
        print(f"❌ 파일을 찾을 수 없습니다: {args.file}")
        sys.exit(1)
        
    upload_to_db(args.file, args.table)