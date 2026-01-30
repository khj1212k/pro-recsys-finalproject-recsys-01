import os
import sys
import json
import pickle
import random
from datetime import datetime

# 프로젝트 루트 경로 설정
current_dir = os.path.dirname(os.path.abspath(__file__)) # 현재 스크립트가 위치한 디렉토리 (scripts/)
project_root = os.path.dirname(current_dir) # 프로젝트 루트 디렉토리 (recommend-engine/)
sys.path.append(project_root)

from bge_m3 import BGEEmbeddingModel
from src.utils.common import load_config

# 표준 카테고리 및 매핑 규칙 정의
STANDARD_CATEGORIES = ["정치", "경제", "사회", "생활문화", "IT과학", "세계", "스포츠"] # 시스템에서 사용하는 표준 카테고리 목록
CATEGORY_MAP = { # 비표준 카테고리를 표준 카테고리로 매핑
    "문화": "생활문화",
    "국제": "세계"
}

def find_news_items(data):
    """
    JSON 데이터 구조를 분석하여 뉴스 아이템 리스트를 찾아냅니다.
    """
    if isinstance(data, list):
        print(f">> [DataCheck] Root is List (len={len(data)})")
        return data

    if isinstance(data, dict):
        print(f">> [DataCheck] Root is Dict. Keys: {list(data.keys())}")
        
        # "newsletters" 키 확인
        if "newsletters" in data and isinstance(data["newsletters"], list):
            print(">> [DataCheck] Found 'newsletters' key. Using this list.")
            return data["newsletters"]

        # 리스트를 값으로 가지는 키 탐색
        for k, v in data.items():
            if isinstance(v, list) and len(v) > 0:
                first_item = v[0]
                if isinstance(first_item, dict) and ('title' in first_item or 'news_letter_id' in first_item):
                    print(f">> [DataCheck] Found news list inside key: '{k}'")
                    return v
        
        # Dict of Dicts 구조 처리
        first_val = next(iter(data.values())) if data else None
        if isinstance(first_val, dict) and 'title' in first_val:
            print(">> [DataCheck] Detected 'Dict of Dicts' structure. Using values().")
            return data.values()

    print(">> [Warning] Could not automatically find news list structure.")
    return []

def normalize_categories(raw_cat):
    """
    카테고리 데이터를 리스트로 변환하고 표준 명칭으로 매핑합니다.
    """
    if not raw_cat:
        return []
    
    # 리스트 또는 문자열 분리
    if isinstance(raw_cat, list):
        cats = raw_cat
    else:
        cats = str(raw_cat).split(',')
        
    # 매핑 및 공백 제거
    normalized = [] # 표준화된 카테고리를 저장할 리스트
    for c in cats:
        c = c.strip()
        # 매핑 테이블에 있으면 변환, 없으면 그대로 사용
        mapped_c = CATEGORY_MAP.get(c, c)
        normalized.append(mapped_c)
        
    # 중복 제거
    return list(set(normalized))

def generate_data():
    print(f">> [ETL] Starting Data Generation & Embedding Process...")
    
    # 1. Config 로드
    config_path = os.path.join(project_root, "configs", "config.yaml") # 설정 파일 경로
    config = load_config(config_path) # 설정 딕셔너리
    
    gen_config = config.get('data_generation', {}) # 데이터 생성 관련 설정
    target_user_count = gen_config.get('num_dummy_users', 30) # 생성할 더미 유저 수 (기본값: 30)
    
    # 2. 원본 데이터 로드
    local_data_path = os.path.join(project_root, "data", "newsletter_share.json") # 로컬 데이터 경로
    if not os.path.exists(local_data_path):
        shared_path = os.path.join(project_root, "../../newsletter_share.json") # 공유 데이터 경로 (대체)
        if os.path.exists(shared_path):
             local_data_path = shared_path
        else:
            raise FileNotFoundError(f"Cannot find newsletter_share.json")

    print(f">> [ETL] Loading raw data from: {local_data_path}")
    with open(local_data_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f) # 원본 JSON 데이터

    # 3. 데이터 구조 탐색
    items_iterator = find_news_items(raw_data) # 뉴스 아이템 이터레이터
    if items_iterator is None:
        items_iterator = []

    news_list = [] # 처리된 뉴스 아이템을 저장할 리스트
    for item in items_iterator:
        if not isinstance(item, dict):
            continue
            
        title = item.get('title', '') # 뉴스 제목
        if not title:
            continue

        # ID 추출 (news_letter_id 우선)
        raw_id = item.get('news_letter_id') or item.get('id')
        final_id = str(raw_id) if raw_id is not None else str(len(news_list) + 1) # 최종 뉴스 ID (문자열)

        # 카테고리 전처리 (표준화 적용)
        raw_cat = item.get('categories') or item.get('category', '')
        normalized_cats = normalize_categories(raw_cat) # 표준화된 카테고리 리스트
        category_str = ", ".join(normalized_cats) # 화면 표시용 카테고리 문자열

        full_text = f"Title: {title}\nCategory: {category_str}\nContent: {item.get('content', '')}" # 임베딩용 전체 텍스트
        
        news_item = { # 뉴스레터 정보 딕셔너리
            "id": final_id,
            "title": title,
            "category": category_str,       # 화면 표시용 (문자열)
            "category_list": normalized_cats, # 로직 처리용 (리스트)
            "content": item.get('content', ''),
            "date": item.get('date', datetime.now().strftime("%Y-%m-%d")),
            "full_text": full_text 
        }
        news_list.append(news_item)
    
    valid_count = len(news_list) # 유효한 뉴스 아이템 수
    print(f">> [ETL] Processed {valid_count} valid news items.")
    if valid_count > 0:
        print(f"   - Sample ID: {news_list[0]['id']} / Cats: {news_list[0]['category_list']}")
    
    if valid_count == 0:
        print("❌ Error: No valid news items found.")
        return

    # 4. 임베딩 수행
    print(f">> [Model] Loading BGE-M3 model...")
    embedder = BGEEmbeddingModel(config['model']) # BGE-M3 임베딩 모델 인스턴스
    
    all_texts = [n['full_text'] for n in news_list] # 임베딩할 텍스트 리스트
    all_ids = [n['id'] for n in news_list] # 뉴스 ID 리스트
    
    print(f">> [Model] Encoding {len(all_texts)} items...")
    embeddings = embedder.encode(all_texts) # 임베딩 결과 Tensor
    
    if embeddings is None:
        print("❌ Error: Embedding returned None.")
        return

    news_vector_map = {} # 뉴스 ID를 키로, 벡터를 값으로 하는 딕셔너리
    for nid, vector in zip(all_ids, embeddings):
        news_vector_map[nid] = vector.cpu()
        
    print(f">> [ETL] Vectorization complete. Map size: {len(news_vector_map)}")

    # 5. 더미 유저 생성 (Multi-Category Logic)
    print(f">> [ETL] Generating {target_user_count} dummy users with Multi-Category Preferences...")
    
    users = [] # 생성된 유저 프로필 리스트
    
    for i in range(1, target_user_count + 1):
        # 유저 선호 카테고리 결정 (1~3개 랜덤 선택)
        num_prefs = random.randint(1, 3) # 선호 카테고리 개수
        user_prefs = random.sample(STANDARD_CATEGORIES, num_prefs) # 선택된 선호 카테고리 리스트
        
        # 뉴스 필터링 (교집합이 하나라도 있으면 후보군)
        # 예: 유저가 ['사회'] 선택 -> 뉴스 ['사회', '경제']는 후보에 포함됨
        candidate_news = [] # 해당 유저의 후보 뉴스 리스트
        user_prefs_set = set(user_prefs) # 빠른 검색을 위한 집합 변환
        
        for n in news_list:
            news_cats_set = set(n['category_list'])
            if not news_cats_set.isdisjoint(user_prefs_set): # 교집합이 있으면 후보에 추가
                candidate_news.append(n)
        
        # 후보군 부족 시 재시도 (현실성 보정)
        retry = 0
        while len(candidate_news) < 1 and retry < 5:
            num_prefs = random.randint(1, 3)
            user_prefs = random.sample(STANDARD_CATEGORIES, num_prefs)
            user_prefs_set = set(user_prefs)
            candidate_news = [n for n in news_list if not set(n['category_list']).isdisjoint(user_prefs_set)]
            retry += 1
            
        # 뉴스 선택
        if candidate_news:
            sample_size = min(len(candidate_news), random.randint(3, 5)) # 선택할 뉴스 개수 (3~5개)
            selected_news = random.sample(candidate_news, k=sample_size) # 선택된 뉴스 리스트
            selected_ids = [n['id'] for n in selected_news] # 선택된 뉴스 ID 리스트
        else:
            selected_ids = [] # Cold Start 상황

        user = { # 유저 프로필 딕셔너리
            "user_id": f"user_{i:03d}",
            "nickname": f"Tester_{i}",
            "birth_year": random.randint(1990, 2005),
            "gender": random.choice(["M", "F"]),
            "preferred_categories": user_prefs,
            "signup_selected_ids": selected_ids
        }
        users.append(user)
    
    # 6. 파일 저장
    output_dir = os.path.join(project_root, "data") # 출력 디렉토리 경로
    os.makedirs(output_dir, exist_ok=True)
    
    # 저장 전 임시 필드 제거
    for n in news_list:
        if 'category_list' in n:
            del n['category_list']

    with open(os.path.join(output_dir, "news.pkl"), "wb") as f:
        pickle.dump(news_list, f)
    with open(os.path.join(output_dir, "users.pkl"), "wb") as f:
        pickle.dump(users, f)
    with open(os.path.join(output_dir, "news_vectors.pkl"), "wb") as f:
        pickle.dump(news_vector_map, f)

    print(f">> [Success] Data generation finished.")

if __name__ == "__main__":
    generate_data()
