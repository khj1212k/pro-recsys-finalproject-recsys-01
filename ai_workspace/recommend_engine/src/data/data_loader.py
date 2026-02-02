# src/data/data_loader.py
import os
import pickle
import math
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from ..utils.common import get_project_root, load_config

# DB 연동 (선택적)
try:
    from sqlalchemy import create_engine, text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

# =============================================================================
# 1. 전역 상수 정의 (Module Level Constants)
# =============================================================================

CATEGORY_ID_TO_NAME = {
    1: "정치", 2: "경제", 3: "IT/과학", 4: "사회",
    5: "생활/문화", 6: "스포츠", 7: "세계"
}

CATEGORY_NAME_TO_ID = {v: k for k, v in CATEGORY_ID_TO_NAME.items()}

NUM_CATEGORIES = 7

# 레거시 호환용
AGE_BAND_TO_IDX = {
    'S1_18_24': 0, 'S2_25_34': 1, 'S3_35_44': 2, 
    'S4_45_54': 3, 'S5_55_64': 4, 'S6_65_': 5
}
GENDER_STR_TO_IDX = {'U': 0, 'M': 1, 'F': 2}


# =============================================================================
# 2. 유틸리티 함수
# =============================================================================

def compute_time_decay(days_ago: float, half_life: float = 7.0, min_weight: float = 0.01) -> float:
    """시간 감쇠 가중치 계산 (지수 감쇠)"""
    if days_ago < 0: return 1.0
    weight = math.pow(0.5, days_ago / half_life)
    return max(weight, min_weight)


# =============================================================================
# 3. 데이터 클래스 정의
# =============================================================================

@dataclass
class ClickEvent:
    """사용자 클릭 로그 이벤트"""
    user_id: int
    news_id: int
    timestamp: datetime = None

@dataclass
class UserProfile:
    """사용자 프로필 데이터 클래스"""
    user_id: int
    age_band_idx: int       # 0-5
    gender_idx: int         # 0-2
    
    # 온보딩 정보
    onboarding_categories: List[int] = field(default_factory=list)
    
    # [New] 계산된 히스토리 임베딩 (Time-decayed average)
    history_embedding: np.ndarray = None 

@dataclass
class NewsItem:
    """뉴스 아이템 데이터 클래스"""
    news_id: int
    title: str
    content: str
    category_ids: List[int]
    embedding: np.ndarray   # BGE-M3 Vector (1024d)
    timestamp: datetime     # 발행 시각


# =============================================================================
# 4. DataLoader 클래스
# =============================================================================

class DataLoader:
    
    def __init__(self, base_path: str = None, config: Dict = None):
        if config is None:
            config = load_config()
        self.config = config
        
        # 데이터 소스 설정
        self.data_source = config.get('data_source', 'file')
        self.use_db = (self.data_source == 'db')

        if self.use_db and not HAS_SQLALCHEMY:
            print("⚠️ SQLAlchemy가 없습니다. 'file' 모드로 전환합니다.")
            self.use_db = False

        # DB 엔진 초기화
        self.engine = None
        if self.use_db:
            db_conf = config['database']
            # [수정] URL 끝에 ?client_encoding=utf8 추가하여 한글 깨짐 방지
            url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
            self.engine = create_engine(url)
            print(f"🔌 DB 연결 설정됨: {url.split('@')[-1]}")

        # 경로 설정
        self.base_path = base_path if base_path else config['data']['base_path']
        if not os.path.isabs(self.base_path):
            self.base_path = os.path.join(get_project_root(), self.base_path)

        # Time Decay 설정
        time_decay_config = config.get('time_decay', {})
        self.news_half_life = time_decay_config.get('news_half_life_days', 7)
        self.min_weight = time_decay_config.get('min_weight', 0.01)

        # 캐시
        self._users_df = None
        self._user_categories_df = None
        self._news_dict = None
        self._ctr_train_df = None
        self._user_profiles = None

    def _get_path(self, filename: str) -> str:
        return os.path.join(self.base_path, filename)

    def _load_from_db(self, query: str, params: dict = None) -> pd.DataFrame:
        if not self.engine:
            raise ConnectionError("DB Engine not initialized")
        with self.engine.connect() as conn:
            return pd.read_sql(text(query), conn, params=params)

    # -------------------------------------------------------------------------
    # 1. Basic Loaders (테이블 이름 소문자 & 인코딩 처리)
    # -------------------------------------------------------------------------

    def load_users(self) -> pd.DataFrame:
        if self._users_df is not None: return self._users_df
        
        if self.use_db:
            # user는 예약어이므로 "user"
            query = 'SELECT user_id, user_gender_code, user_birth_year FROM "user"'
            self._users_df = self._load_from_db(query)
        else:
            path = self._get_path(self.config['data']['users'])
            self._users_df = pd.read_csv(path)
            
        return self._users_df

    def load_user_preferred_categories(self) -> pd.DataFrame:
        if self._user_categories_df is not None: return self._user_categories_df
        
        if self.use_db:
            query = 'SELECT user_id, category_id FROM user_preferred_categories'
            self._user_categories_df = self._load_from_db(query)
        else:
            path = self._get_path(self.config['data']['user_preferred_categories'])
            self._user_categories_df = pd.read_csv(path)
            
        return self._user_categories_df

    def load_embedded_news(self) -> Dict[int, NewsItem]:
        if self._news_dict is not None: return self._news_dict
        
        # 1. 임베딩 로드 (PKL)
        pkl_path = self._get_path(self.config['data']['embedded_news'])
        with open(pkl_path, 'rb') as f:
            pkl_data = pickle.load(f)

        self._news_dict = {}
        
        if self.use_db:
            # 2. 메타데이터 로드 (DB)
            query = """
                SELECT news_letter_id, news_letter_title, news_letter_content, news_letter_created_at 
                FROM news_letter
            """
            meta_df = self._load_from_db(query)
            
            # 카테고리 로드
            cat_query = 'SELECT news_letter_id, category_id FROM news_letter_categories'
            cat_df = self._load_from_db(cat_query)
            cat_map = cat_df.groupby('news_letter_id')['category_id'].apply(list).to_dict()
            
            for _, row in meta_df.iterrows():
                nid = int(row['news_letter_id'])
                
                # 임베딩 매핑
                if nid in pkl_data:
                    emb = pkl_data[nid].get('embedding', np.zeros(1024))
                else:
                    emb = np.zeros(1024)
                
                if isinstance(emb, list): emb = np.array(emb, dtype=np.float32)

                self._news_dict[nid] = NewsItem(
                    news_id=nid,
                    title=row['news_letter_title'],
                    content=row['news_letter_content'],
                    category_ids=cat_map.get(nid, []),
                    embedding=emb,
                    timestamp=pd.to_datetime(row['news_letter_created_at'])
                )
        else:
            # File 모드
            cat_path = self._get_path(self.config['data']['newsletter_categories_map'])
            cat_df = pd.read_csv(cat_path)
            cat_map = cat_df.groupby('news_letter_id')['category_id'].apply(list).to_dict()
            
            for nid, item_dict in pkl_data.items():
                emb = np.array(item_dict.get('embedding', np.zeros(1024)), dtype=np.float32)
                self._news_dict[nid] = NewsItem(
                    news_id=nid,
                    title=item_dict.get('title', ''),
                    content=item_dict.get('content', ''),
                    category_ids=cat_map.get(nid, []),
                    embedding=emb,
                    timestamp=pd.to_datetime(item_dict.get('created_at', datetime.now()))
                )
                
        return self._news_dict

    def load_ctr_logs(self, split: str = 'train') -> pd.DataFrame:
        if split == 'train' and self._ctr_train_df is not None:
            return self._ctr_train_df
            
        if self.use_db:
            # JOIN 쿼리: 로그와 뉴스레터 발행일을 함께 조회
            query = """
                SELECT 
                    l.user_id, 
                    l.news_letter_id, 
                    l.created_at as log_timestamp,
                    n.news_letter_created_at as pub_date
                FROM user_newsletter_ctr_log l
                JOIN news_letter n ON l.news_letter_id = n.news_letter_id
            """
            
            print(f"🔍 [Debug] DB에서 로그 조회 시작 ({split})...")
            df = self._load_from_db(query)
            print(f"🔍 [Debug] Raw Logs Loaded: {len(df)} rows")
            
            if df.empty:
                print("❌ [Error] DB에서 가져온 로그가 0건입니다! JOIN 조건을 만족하는 데이터가 없거나 테이블이 비어있습니다.")
                return pd.DataFrame(columns=['user_id', 'news_letter_id', 'timestamp', 'is_clicked', 'pub_date'])

            df['is_clicked'] = 1 
            df['timestamp'] = pd.to_datetime(df['log_timestamp'])
            df['pub_date'] = pd.to_datetime(df['pub_date'])
            
            # [디버깅] 실제 데이터의 날짜 범위 출력
            min_date = df['pub_date'].min()
            max_date = df['pub_date'].max()
            print(f"📅 [Data Check] 뉴스 발행일 범위: {min_date} ~ {max_date}")

            threshold_str = self.config.get('data', {}).get('validation_threshold_date', None)
            
            if threshold_str:
                threshold_date = pd.to_datetime(threshold_str)
                print(f"✂️ [Filter] 기준 날짜: {threshold_date}")
                
                if split == 'train':
                    # 기준일 미만
                    filtered_df = df[df['pub_date'] < threshold_date].copy()
                    print(f"   👉 Train Set (< {threshold_date}): {len(filtered_df)} rows selected")
                    self._ctr_train_df = filtered_df
                    return filtered_df
                else:
                    # 기준일 이상
                    filtered_df = df[df['pub_date'] >= threshold_date].copy()
                    print(f"   👉 Valid Set (>= {threshold_date}): {len(filtered_df)} rows selected")
                    return filtered_df
            
            else:
                # Fallback: 날짜 설정 없음
                print("⚠️ 날짜 설정이 없어 비율(80:20)로 분할합니다.")
                df = df.sort_values('timestamp')
                split_idx = int(len(df) * 0.8)
                
                if split == 'train':
                    self._ctr_train_df = df.iloc[:split_idx].copy()
                    return self._ctr_train_df
                else:
                    return df.iloc[split_idx:].copy()

        else:
            # File 모드
            filename = self.config['data'].get(f'ctr_logs_{split}', f'ctr_logs_{split}.csv')
            df = pd.read_csv(self._get_path(filename))
            if 'timestamp' in df.columns:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
            

    def load_newsletter_categories_map(self) -> pd.DataFrame:
        if self.use_db:
            query = 'SELECT news_letter_id, category_id FROM news_letter_categories'
            return self._load_from_db(query)
        else:
            path = self._get_path(self.config['data']['newsletter_categories_map'])
            return pd.read_csv(path)

    # -------------------------------------------------------------------------
    # 2. User Profile Builder
    # -------------------------------------------------------------------------

    def build_user_profiles(self) -> Dict[int, UserProfile]:
        if self._user_profiles is not None: return self._user_profiles
            
        print("👤 사용자 프로필 빌드 중 (History Embedding 계산 포함)...")
        
        users_df = self.load_users()
        pref_cat_df = self.load_user_preferred_categories()
        news_dict = self.load_embedded_news()
        logs_df = self.load_ctr_logs('train')
        
        # logs_df가 비었을 때 {} 대신 None 할당 (GroupBy 객체와 구분)
        user_logs_map = None
        if not logs_df.empty:
            user_logs_map = logs_df.groupby('user_id')
        
        user_cat_map = pref_cat_df.groupby('user_id')['category_id'].apply(list).to_dict()
        
        profiles = {}
        now = datetime.now()
        
        for _, row in users_df.iterrows():
            uid = int(row['user_id'])
            
            # Age
            age = now.year - int(row['user_birth_year'])
            if age < 18: age_band = 0
            elif age <= 24: age_band = 1
            elif age <= 34: age_band = 2
            elif age <= 44: age_band = 3
            elif age <= 54: age_band = 4
            else: age_band = 5
            
            # Gender
            gender_idx = int(row['user_gender_code']) if 'user_gender_code' in row else 0
            
            # History Embedding
            hist_emb = np.zeros(1024)
            
            # user_logs_map이 None이 아니고, 해당 유저 그룹이 있을 때만 접근
            if user_logs_map is not None and uid in user_logs_map.groups:
                u_logs = user_logs_map.get_group(uid)
                vectors = []
                weights = []
                
                for _, log in u_logs.iterrows():
                    nid = int(log['news_letter_id'])
                    if nid in news_dict:
                        vec = news_dict[nid].embedding
                        
                        days_ago = 0
                        if 'timestamp' in log and pd.notnull(log['timestamp']):
                            days_ago = (now - log['timestamp']).days
                        
                        w = compute_time_decay(days_ago, self.news_half_life, self.min_weight)
                        
                        vectors.append(vec)
                        weights.append(w)
                
                if vectors:
                    hist_emb = np.average(vectors, axis=0, weights=weights)

            profiles[uid] = UserProfile(
                user_id=uid,
                age_band_idx=age_band,
                gender_idx=gender_idx,
                onboarding_categories=user_cat_map.get(uid, []),
                history_embedding=hist_emb
            )
            
        self._user_profiles = profiles
        print(f"✅ 프로필 생성 완료: {len(profiles)}명")
        return self._user_profiles

    # Helper methods for LGBMDataset
    def get_all_news_ids(self) -> List[int]:
        return list(self.load_embedded_news().keys())

    def get_all_user_ids(self) -> List[int]:
        return self.load_users()['user_id'].tolist()
    
    def get_user_history_embedding(self, user_id: int) -> np.ndarray:
        if self._user_profiles is None:
            self.build_user_profiles()
        profile = self._user_profiles.get(user_id)
        if profile and profile.history_embedding is not None:
            return profile.history_embedding
        return np.zeros(1024)