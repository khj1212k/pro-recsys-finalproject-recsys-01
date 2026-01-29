import os
import pickle


def get_data_path(filename):
    """
    데이터 파일의 절대 경로를 반환.
    파일이 없으면 FileNotFoundError 발생.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__)) # 현재 파일 위치 (src/data/)
    project_root = os.path.dirname(os.path.dirname(current_dir)) # 프로젝트 루트 (recommend-engine/)
    
    path = os.path.join(project_root, 'data', filename) # 데이터 파일 절대 경로
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Data file not found: {path}. \n"
            f"Please run 'python scripts/generate_data.py' to generate embeddings first."
        )
    return path


def get_todays_news_candidates():
    """
    뉴스 메타데이터 로드.
    
    반환: 뉴스 정보 리스트 (id, title, category 등 포함)
    """
    pkl_path = get_data_path("news.pkl") # 뉴스 데이터 파일 경로
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)


def get_user_profiles():
    """
    유저 프로필 로드.
    
    반환: 유저 정보 List[dict] (user_id, nickname, preferred_categories 등 포함)
    """
    pkl_path = get_data_path("users.pkl") # 유저 데이터 파일 경로
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)


def load_news_vectors():
    """
    사전 계산된 뉴스 벡터 맵 로드.
    
    반환: {news_id (str): torch.Tensor (cpu)} 형태의 딕셔너리
    """
    pkl_path = get_data_path("news_vectors.pkl") # 뉴스 벡터 파일 경로
    print(f">> [Data] Loading pre-computed vectors from {pkl_path}...")
    with open(pkl_path, 'rb') as f:
        return pickle.load(f)
