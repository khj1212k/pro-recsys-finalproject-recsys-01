import sys
import os
import torch
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

# 임베딩된 데이터 불러오기
from src.utils.common import load_config
import src.data.loader as data_loader

# 추천 모델 불러오기
from src.core.scorers.max_pooling import MaxPoolingScorer
from src.core.scorers.weighted_avg import WeightedAverageScorer
from src.core.scorers.attention import AttentionScorer
from src.core.reranker import MMRReranker

# scoring 이름에 대응하는 클래스를 불러오는 함수
def get_scorer_class(method_name):
    """
    Scoring 메서드 이름에 해당하는 클래스 반환.
    지원 메서드: max_pooling, weighted_avg, attention
    """
    mapping = {
        "max_pooling": MaxPoolingScorer,
        "weighted_avg": WeightedAverageScorer,
        "attention": AttentionScorer
    }
    if method_name not in mapping:
        raise ValueError(f"Unknown scoring method: {method_name}")
    return mapping[method_name]

def main():
    """
    뉴스 추천 엔진 메인 함수 (Serving Mode).
    1. 설정 및 데이터 로드
    2. 후보 뉴스 벡터 준비
    3. 사용자별 추천 수행 (Scoring + Reranking)
    4. 결과 파일 저장
    """
    # 결과값 저장을 위한 코드
    os.makedirs("results", exist_ok=True) # 저장 위치
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S") # 결과 저장 시점 (예: 20260129_110215)
    result_filename = f"results/rec_{timestamp}.txt" # 결과 파일 이름 (예: results/rec_20260129_110215.txt)
    original_stdout = sys.stdout # 원래 stdout을 백업 (나중에 복원용)

    print(f">> [System] Recommendation Engine (Serving Mode) Started.")
    
    try:
        with open(result_filename, "w", encoding='utf-8') as result_file:
            sys.stdout = result_file 

            # 1. config.yaml로부터 설정값 불러오기
            config = load_config("configs/config.yaml") # 전체 설정 딕셔너리
            rec_cfg = config['recommendation'] # 추천 관련 설정 부분만 추출
            
            method = rec_cfg.get('method', 'weighted_avg') # content 기반 추천 방법. 기본값: 'weighted_avg'
            use_mmr = rec_cfg.get('use_mmr', True) # MMR 사용 여부. 기본값: True
            top_k = rec_cfg.get('top_k', 5) # 추천할 뉴스레터 수. 기본값: 5
            mmr_lambda = rec_cfg.get('mmr_lambda', 0.7) # MMR lambda 값. 기본값 0.7
            
            # Device 설정
            device_str = config['model'].get('device', 'cpu')
            if device_str == "cuda" and torch.cuda.is_available():
                device = torch.device("cuda")
            else:
                device = torch.device("cpu")
            
            scorer_class = get_scorer_class(method) # 선택된 scoring 방법에 해당하는 클래스 (예: WeightedAverageScorer)

            print("=" * 70)
            print(f"       [News Recommendation Serving Log]")
            print(f"       Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"       Mode: Inference Only (Vector Loading)")
            print("=" * 70)
            print(f"1. System Environment")
            print(f"   - Runtime Device : {device}")
            print("-" * 70)
            print(f"2. Strategy")
            print(f"   - Method         : {method}")
            print(f"   - Reranker       : {'MMR' if use_mmr else 'None'}")
            print("=" * 70)

            # 2. data loading
            vector_map = data_loader.load_news_vectors() # 뉴스 ID를 키로, 임베딩 벡터를 값으로 하는 딕셔너리
            candidates = data_loader.get_todays_news_candidates() # 오늘의 추천 후보 뉴스 리스트 (각 항목은 id, title, category 등 포함)
            users = data_loader.get_user_profiles() # 사용자 프로필 리스트 (user_id, nickname, preferred_categories, signup_selected_ids 등 포함)
            news_info_map = {item['id']: item for item in candidates} # 뉴스 ID를 키로, 뉴스 정보를 값으로 하는 딕셔너리

            # 3. 후보 벡터 스택
            candidate_ids = [n['id'] for n in candidates] # 모든 후보 뉴스의 ID 리스트
            candidate_vectors_list = [vector_map[nid] for nid in candidate_ids if nid in vector_map] # 후보 뉴스의 임베딩 벡터 리스트
            
            if not candidate_vectors_list:
                raise ValueError("No candidate vectors found.")
                
            all_candidate_vectors = torch.stack(candidate_vectors_list).to(device) # 모든 후보 뉴스 벡터를 하나의 텐서로 스택 (shape: [num_candidates, embedding_dim])
            print(f">> [Data] Loaded {len(all_candidate_vectors)} candidate vectors to {device}.")

            # 4. init engine
            scorer = scorer_class() # scoring 클래스 인스턴스 (사용자 벡터와 후보 벡터 간 유사도 계산)
            reranker = MMRReranker(lambda_param=mmr_lambda) # MMR 재순위화 엔진 (다양성을 고려한 재정렬)

            # 5. recommendation Loop
            print(f">> [Serve] Processing {len(users)} users request...")
            
            for user in users:
                print(f"\n========================================================")
                print(f"User: {user['nickname']} (ID: {user['user_id']})")
                print(f" - Info: {user['birth_year']} / {user['gender']}")
                print(f" - Interested Categories: {user['preferred_categories']}")
                
                selected_ids = user['signup_selected_ids'] # 사용자가 회원가입 시 선택한 뉴스 ID 리스트
                print(f" - Signup Selected News IDs: {selected_ids}")

                # 사용자 히스토리 벡터 구성
                history_items = [] # 사용자가 선택한 뉴스의 상세 정보 리스트 (title, category 등)
                history_vectors_list = [] # 사용자가 선택한 뉴스의 임베딩 벡터 리스트
                
                for nid in selected_ids:
                    nid_str = str(nid) # 뉴스 ID를 문자열로 변환 (딕셔너리 키와 타입 매칭용)
                    if nid_str in news_info_map and nid_str in vector_map:
                        history_items.append(news_info_map[nid_str])
                        history_vectors_list.append(vector_map[nid_str])
                
                # weight calculation (추천 뉴스레터별 가중치 확인용)
                weights_list = scorer.calculate_weights( # 사용자 선호 카테고리 기반으로 각 히스토리 아이템의 가중치 계산 (예: [1.0, 1.5, 1.0])
                    history_items, 
                    user['preferred_categories']
                )

                # 사용자 선호 뉴스 정보 출력 (디버깅용)
                print(f"   [User's Initially Selected News]")
                if not history_items:
                    print("    * No information available (Cold Start)")
                    print("-" * 56)
                    continue
                
                for i, item in enumerate(history_items):
                    w_str = "" # 가중치 출력용 문자열 (예: " (weight: 1.5)")
                    if weights_list:
                        w_str = f" (weight: {weights_list[i]:.1f})"
                    print(f"    * [{item['category']}] {item['title']}{w_str}")
                
                print("-" * 56)
                    
                user_vectors = torch.stack(history_vectors_list).to(device) # 사용자 히스토리 벡터들을 텐서로 스택 (shape: [num_history, embedding_dim])
                weights_tensor = None # 가중치 텐서 초기화
                if weights_list:
                    weights_tensor = torch.tensor(weights_list, dtype=torch.float32).to(device) # 가중치 리스트를 텐서로 변환 (shape: [num_history])

                # 이미 본 뉴스 필터링
                watched_set = set(str(nid) for nid in selected_ids) # 사용자가 이미 본 뉴스 ID 집합 (중복 제거 및 빠른 검색용)
                valid_indices = [i for i, cid in enumerate(candidate_ids) if cid not in watched_set] # 아직 보지 않은 후보 뉴스의 인덱스 리스트
                
                if not valid_indices:
                    print("  * All candidates have already been watched.")
                    continue
                
                filtered_vectors = all_candidate_vectors[valid_indices] # 필터링된 후보 뉴스의 벡터 텐서 (shape: [num_valid, embedding_dim])
                filtered_ids = [candidate_ids[i] for i in valid_indices] # 필터링된 후보 뉴스의 ID 리스트

                # Scoring
                scores = scorer.score(user_vectors, filtered_vectors, weights=weights_tensor) # 각 후보 뉴스에 대한 유사도 점수 텐서 (shape: [num_valid])

                # Reranking
                if use_mmr:
                    final_results = reranker.rerank(scores, filtered_vectors, filtered_ids, top_k) # MMR 재순위화 결과 (다양성 고려, 각 항목: {"news_id": ..., "score": ...})
                else:
                    values, indices = torch.topk(scores, k=min(top_k, len(scores))) # 상위 k개 점수와 인덱스
                    final_results = [{"news_id": filtered_ids[idx], "score": val.item()} for val, idx in zip(values, indices)] # 최종 추천 결과 리스트

                print(f"   [Recommendations (Method: {method})]")
                for rank, rec in enumerate(final_results, 1):
                    news_info = news_info_map[rec['news_id']] # 추천된 뉴스의 상세 정보 (title, category 등)
                    
                    user_prefs = user['preferred_categories'] # 사용자가 선호하는 카테고리 (리스트 또는 문자열)
                    news_cats = str(news_info['category']) # 뉴스의 카테고리를 문자열로 변환
                    is_preferred_cat = False # 선호 카테고리 일치 여부 플래그
                    if isinstance(user_prefs, list):
                        is_preferred_cat = any(p in news_cats for p in user_prefs)
                    elif isinstance(user_prefs, str):
                        is_preferred_cat = user_prefs in news_cats
                        
                    cat_mark = "★" if is_preferred_cat else " " # 선호 카테고리 일치 시 별표 마크
                    
                    print(f"  [Rank {rank}] {cat_mark} [{news_info['category']}] {news_info['title']}")
                    print(f"        (Score: {rec['score']:.4f})")

            print("\n=== Recommendation Serving Complete ===")

    except Exception as e:
        sys.stdout = original_stdout
        print(f"\n❌ Serving Error: {e}")
        raise e
    finally:
        sys.stdout = original_stdout
        print(f">> [System] Log saved to {result_filename}")

if __name__ == "__main__":
    main()
