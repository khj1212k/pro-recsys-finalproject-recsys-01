import os
import pickle
import torch

# 데이터 파일 경로 유틸리티
def get_data_path(filename):
    current_dir = os.path.dirname(os.path.abspath(__file__)) # src/data/
    project_root = os.path.dirname(os.path.dirname(current_dir)) # recommend-engine/
    
    path = os.path.join(project_root, 'data', filename)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Data file not found: {path}. \n"
            f"Please run 'python scripts/generate_data.py' to generate embeddings first."
        )
    return path

def get_todays_news_candidates():
    """뉴스 메타데이터 로드 (제목, 카테고리 등)"""
    pkl_path = get_data_path("news.pkl")
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)

def get_user_profiles():
    """유저 정보 로드"""
    pkl_path = get_data_path("users.pkl")
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)

def load_news_vectors():
    """
    [핵심] 사전 계산된 뉴스 벡터 맵(Dictionary) 로드
    Returns:
        dict: {news_id (str): torch.Tensor (cpu)}
    """
    pkl_path = get_data_path("news_vectors.pkl")
    print(f">> [Data] Loading pre-computed vectors from {pkl_path}...")
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)
