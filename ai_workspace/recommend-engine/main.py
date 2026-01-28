import sys
import os
import torch
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from src.utils.common import load_config
import src.data.loader as data_loader

# [Core Modules]
from src.core.scorers.max_pooling import MaxPoolingScorer
from src.core.scorers.weighted_avg import WeightedAverageScorer
from src.core.scorers.attention import AttentionScorer
from src.core.reranker import MMRReranker

def get_scorer_class(method_name):
    mapping = {
        "max_pooling": MaxPoolingScorer,
        "weighted_avg": WeightedAverageScorer,
        "attention": AttentionScorer
    }
    if method_name not in mapping:
        raise ValueError(f"Unknown scoring method: {method_name}")
    return mapping[method_name]

def main():
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"results/rec_{timestamp}.txt"
    original_stdout = sys.stdout

    print(f">> [System] Recommendation Engine (Serving Mode) Started.")
    
    try:
        with open(log_filename, "w", encoding='utf-8') as log_file:
            sys.stdout = log_file 

            # 1. Config Load
            config = load_config("configs/config.yaml")
            rec_cfg = config['recommendation']
            
            method = rec_cfg.get('method', 'weighted_avg')
            use_mmr = rec_cfg.get('use_mmr', True)
            top_k = rec_cfg.get('top_k', 5)
            mmr_lambda = rec_cfg.get('mmr_lambda', 0.7)
            
            # Device 설정
            device_str = config['model'].get('device', 'cpu')
            if device_str == "cuda" and torch.cuda.is_available():
                device = torch.device("cuda")
            elif device_str == "mps" and torch.backends.mps.is_available():
                device = torch.device("mps")
            else:
                device = torch.device("cpu")
            
            scorer_class = get_scorer_class(method)

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

            # 2. Data Loading
            vector_map = data_loader.load_news_vectors()
            candidates = data_loader.get_todays_news_candidates()
            users = data_loader.get_user_profiles()
            news_info_map = {item['id']: item for item in candidates}

            # 3. Candidate Vectors Stack
            candidate_ids = [n['id'] for n in candidates]
            candidate_vectors_list = [vector_map[nid] for nid in candidate_ids if nid in vector_map]
            
            if not candidate_vectors_list:
                raise ValueError("No candidate vectors found.")
                
            all_candidate_vectors = torch.stack(candidate_vectors_list).to(device)
            print(f">> [Data] Loaded {len(all_candidate_vectors)} candidate vectors to {device}.")

            # 4. Init Engine
            scorer = scorer_class()
            reranker = MMRReranker(lambda_param=mmr_lambda)

            # 5. Recommendation Loop
            print(f">> [Serve] Processing {len(users)} users request...")
            
            for user in users:
                print(f"\n========================================================")
                print(f"User: {user['nickname']} (ID: {user['user_id']})")
                print(f" - Info: {user['birth_year']}년생 / {user['gender']}")
                print(f" - Interested Categories: {user['preferred_categories']}")
                
                selected_ids = user['signup_selected_ids']
                print(f" - Signup Selected News IDs: {selected_ids}")

                # History Vector Construction
                history_items = []
                history_vectors_list = []
                
                for nid in selected_ids:
                    nid_str = str(nid)
                    if nid_str in news_info_map and nid_str in vector_map:
                        history_items.append(news_info_map[nid_str])
                        history_vectors_list.append(vector_map[nid_str])
                
                # Weight Calculation (For Log Display)
                weights_list = scorer.calculate_weights(
                    history_items, 
                    user['preferred_categories']
                )

                # [History 출력 복구]
                print(f"   [History (사용자가 선택한 뉴스)]")
                if history_items:
                    for i, item in enumerate(history_items):
                        w_str = ""
                        if weights_list:
                            w_str = f" (Weight: {weights_list[i]:.1f})"
                        print(f"    * [{item['category']}] {item['title']}{w_str}")
                else:
                    print("    * 정보 없음 (Cold Start)")
                
                print("-" * 56)

                if not history_vectors_list:
                    print("  * Cold Start: No valid history.")
                    continue
                    
                user_vectors = torch.stack(history_vectors_list).to(device)
                weights_tensor = None
                if weights_list:
                    weights_tensor = torch.tensor(weights_list, dtype=torch.float32).to(device)

                # Filter watched
                watched_set = set(str(nid) for nid in selected_ids)
                valid_indices = [i for i, cid in enumerate(candidate_ids) if cid not in watched_set]
                
                if not valid_indices:
                    print("  * All candidates watched.")
                    continue
                
                filtered_vectors = all_candidate_vectors[valid_indices]
                filtered_ids = [candidate_ids[i] for i in valid_indices]

                # Scoring
                scores = scorer.score(user_vectors, filtered_vectors, weights=weights_tensor)

                # Reranking
                if use_mmr:
                    final_results = reranker.rerank(scores, filtered_vectors, filtered_ids, top_k)
                else:
                    values, indices = torch.topk(scores, k=min(top_k, len(scores)))
                    final_results = [{"news_id": filtered_ids[idx], "score": val.item()} for val, idx in zip(values, indices)]

                # [Output 포맷 복구]
                print(f"   [Recommendations (Method: {method})]")
                for rank, rec in enumerate(final_results, 1):
                    news_info = news_info_map[rec['news_id']]
                    
                    user_prefs = user['preferred_categories']
                    news_cats = str(news_info['category'])
                    is_preferred_cat = False
                    if isinstance(user_prefs, list):
                        is_preferred_cat = any(p in news_cats for p in user_prefs)
                    elif isinstance(user_prefs, str):
                        is_preferred_cat = user_prefs in news_cats
                        
                    cat_mark = "★" if is_preferred_cat else " "
                    
                    print(f"  [{rank}위] {cat_mark} [{news_info['category']}] {news_info['title']}")
                    print(f"        (Score: {rec['score']:.4f})")

            print("\n=== Serving Complete ===")

    except Exception as e:
        sys.stdout = original_stdout
        print(f"\n❌ Serving Error: {e}")
        raise e
    finally:
        sys.stdout = original_stdout
        print(f">> [System] Log saved to {log_filename}")

if __name__ == "__main__":
    main()
