# src/data/data_loader.py
import bisect
import os
import pickle
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from sqlalchemy import create_engine, text

from ..utils.common import get_project_root, load_config, get_logger

# 로거 설정
logger = get_logger("DataLoader")

# =============================================================================
# 데이터 클래스 정의
# =============================================================================

@dataclass
class UserProfile:
    """사용자 프로필 데이터 클래스"""
    user_id: int
    age_band_idx: int       # (현재 DB에 없으면 기본값 처리)
    gender_idx: int         # (현재 DB에 없으면 기본값 처리)
    onboarding_categories: List[int] = field(default_factory=list)
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


def resolve_db_config(yaml_db_conf: Dict = None) -> Dict[str, str]:
    """DB 접속 정보를 결정한다. 환경변수(DB_HOST 등)가 있으면 최우선으로 쓰고,
    없으면 config.yaml의 database 섹션 값으로 폴백한다.

    ai_workspace/db/connection.py(상위 파이프라인)와 동일한 환경변수 이름을 공유해,
    두 서브 프로젝트가 같은 .env 하나로 동일한 DB를 가리키도록 한다. 이전에는
    config.yaml에 비밀번호(recsyspeople)가 평문으로 하드코딩되어 있었다.
    """
    yaml_db_conf = yaml_db_conf or {}
    return {
        "host": os.getenv("DB_HOST", yaml_db_conf.get("host", "localhost")),
        "port": os.getenv("DB_PORT", str(yaml_db_conf.get("port", 5432))),
        "user": os.getenv("DB_USER", yaml_db_conf.get("user", "postgres")),
        "password": os.getenv("DB_PASSWORD", yaml_db_conf.get("password", "")),
        "dbname": os.getenv("DB_NAME", yaml_db_conf.get("dbname", "final_db")),
    }


# =============================================================================
# DataLoader 클래스
# =============================================================================

class DataLoader:

    def __init__(self, config: Dict = None):
        if config is None:
            self.config = load_config()
        else:
            self.config = config

        # DB 연결 설정 (환경변수가 config.yaml보다 우선)
        db_conf = resolve_db_config(self.config.get('database'))
        # URL 끝에 client_encoding 추가
        url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
        
        try:
            self.engine = create_engine(url)
            logger.info(f"🔌 DB 연결 초기화됨: {db_conf['host']}/{db_conf['dbname']}")
        except Exception as e:
            logger.error(f"❌ DB 연결 실패: {e}")
            raise e

        # 캐시 변수
        self._news_dict = None
        self._user_profiles = None

    def _parse_pgvector(self, value: Any) -> np.ndarray:
        """
        PostgreSQL vector 컬럼 값을 numpy array로 변환한다.

        DataLoader는 SQLAlchemy 엔진으로 자체 연결을 맺으므로(ai_workspace/db/
        connection.py의 register_vector와는 별개 연결) 보통 문자열("[0.1,0.2,...]")로
        온다. 하지만 pgvector.psycopg2.register_vector가 등록된 연결로 읽으면
        (ai_workspace/core/user_embedder.py 등 raw psycopg2 경로) 값이
        pgvector.Vector 객체나 numpy.ndarray로 올 수 있다 - 어느 쪽이든 안전하게
        처리한다. 특히 ndarray/Vector를 str()로 강제 변환하면 numpy가 큰 배열
        (1024차원)을 "..."로 요약 표기해 값이 깨지므로 그렇게 하면 안 된다.
        예: "[0.1, 0.2, ...]" -> np.array([0.1, 0.2, ...])
        """
        if value is None:
            return np.zeros(1024, dtype=np.float32)

        if isinstance(value, np.ndarray):
            return value.astype(np.float32, copy=False)

        if isinstance(value, (list, tuple)):
            return np.array(value, dtype=np.float32)

        # pgvector.Vector 등 to_numpy()를 제공하는 래퍼 (duck-typing - 이
        # 서브 프로젝트는 pgvector를 의존성으로 두지 않으므로 직접 import하지 않는다)
        if hasattr(value, "to_numpy"):
            return value.to_numpy().astype(np.float32, copy=False)

        vector_str = str(value)
        if not vector_str:
            return np.zeros(1024, dtype=np.float32)

        try:
            # 1. 불필요한 괄호 및 공백 제거
            clean_str = vector_str.replace('[', '').replace(']', '').replace('{', '').replace('}', '').strip()
            
            # 2. 쉼표로 분리
            if ',' in clean_str:
                parts = clean_str.split(',')
            else:
                # 구분자가 쉼표가 아닌 경우(ex. 공백)
                parts = clean_str.split()
                
            # 3. float 변환
            return np.array([float(x) for x in parts if x], dtype=np.float32)
            
        except Exception as e:
            logger.warning(f"⚠️ 벡터 파싱 실패: {e} (Zero vector 반환)")
            return np.zeros(1024, dtype=np.float32)

    def _load_from_db(self, query: str, params: dict = None) -> pd.DataFrame:
        with self.engine.connect() as conn:
            return pd.read_sql(text(query), conn, params=params)

    # -------------------------------------------------------------------------
    # 1. Data Loaders (Core)
    # -------------------------------------------------------------------------

    def load_embedded_news(self) -> Dict[int, NewsItem]:
        """
        뉴스레터 메타데이터 + 임베딩 + 카테고리를 DB에서 로드하여 NewsItem 객체로 반환
        """
        if self._news_dict is not None: 
            return self._news_dict
        
        logger.info("📡 뉴스레터 데이터 로딩 중 (메타데이터 + 임베딩)...")
        
        # 1. 뉴스 메타데이터 및 임베딩 조회 (JOIN 없이 단일 테이블 조회)
        # 안정성을 위해 날짜 필터 없이 로드 (Log에 있는 과거 뉴스 대응)
        news_query = """
            SELECT 
                news_letter_id, 
                news_letter_title, 
                news_letter_content, 
                news_letter_embedding, 
                news_letter_created_at 
            FROM news_letter
        """
        news_df = self._load_from_db(news_query)
        
        # 2. 뉴스 카테고리 매핑 조회
        cat_query = "SELECT news_letter_id, category_id FROM news_letter_categories"
        cat_df = self._load_from_db(cat_query)
        
        # 메모리 상에서 매핑 (뉴스 ID -> 카테고리 ID 리스트)
        cat_map = cat_df.groupby('news_letter_id')['category_id'].apply(list).to_dict()
        
        self._news_dict = {}
        for _, row in news_df.iterrows():
            nid = int(row['news_letter_id'])
            
            # 임베딩 파싱
            emb_vec = self._parse_pgvector(row['news_letter_embedding'])
            
            # 카테고리 매핑
            cats = cat_map.get(nid, [])
            
            self._news_dict[nid] = NewsItem(
                news_id=nid,
                title=row['news_letter_title'],
                content=row['news_letter_content'],
                category_ids=cats,
                embedding=emb_vec,
                timestamp=pd.to_datetime(row['news_letter_created_at'])
            )
            
        logger.info(f"✅ 뉴스 데이터 로드 완료: {len(self._news_dict)}건")
        
        # (선택) pkl 캐싱 로직은 필요하다면 유지, 여기서는 DB 우선이므로 생략 가능하나
        # 피처 엔지니어링 속도를 위해 로컬 캐싱을 원하면 유지. 일단은 DB Direct로 구현.
        return self._news_dict

    def load_ctr_logs(self, split: str = None) -> pd.DataFrame:
        """
        사용자 클릭 로그 로드 (최근 N일치만)
        split 인자는 호환성을 위해 유지하지만, 실제 분할은 main_lgbm.py에서 수행함.
        여기서는 '전체 유효 기간'의 로그를 다 가져옴.
        """
        days = self.config['data'].get('max_history_days', 28)
        
        logger.info(f"📡 최근 {days}일간의 클릭 로그 조회 중...")
        
        # PostgreSQL Interval 문법 사용
        query = f"""
            SELECT 
                l.user_id, 
                l.news_letter_id, 
                l.created_at as timestamp
            FROM user_newsletter_ctr_log l
            WHERE l.created_at >= NOW() - INTERVAL '{days} DAY'
        """
        
        df = self._load_from_db(query)
        
        # timestamp 변환
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            
        logger.info(f"✅ 로그 로드 완료: {len(df)}건")
        return df

    def load_user_preferred_categories(self) -> pd.DataFrame:
        """사용자 선호 카테고리 (Static Snapshot)"""
        query = "SELECT user_id, category_id FROM user_preferred_categories"
        return self._load_from_db(query)

    # -------------------------------------------------------------------------
    # 2. User Profile Builder
    # -------------------------------------------------------------------------

    def _ensure_history_index(self, logs_df: pd.DataFrame) -> None:
        """logs_df를 user_id별 시간순 정렬 배열로 1회만 사전 분할하고,
        compute_history_embedding()의 (user_id, cutoff_time) 메모이즈 캐시를 준비한다.

        logs_df 객체가 바뀌면(id() 기준) 인덱스/캐시를 다시 구축한다. 같은 로그로
        여러 유저 x 여러 cutoff를 반복 조회하는 추론/학습 경로에서, 매 호출마다
        DataFrame을 필터링하지 않고 이 사전 분할된 배열을 재사용한다.
        """
        key = id(logs_df)
        if getattr(self, "_history_index_key", None) == key:
            return

        self._history_index_key = key
        self._history_embedding_cache: Dict[Any, np.ndarray] = {}
        index: Dict[int, Any] = {}
        if logs_df is not None and not logs_df.empty:
            for uid, group in logs_df.groupby('user_id'):
                sorted_group = group.sort_values('timestamp')
                index[int(uid)] = (
                    sorted_group['timestamp'].tolist(),
                    sorted_group['news_letter_id'].tolist(),
                )
        self._history_user_logs = index

    def _compute_history_embedding_uncached(
        self, user_id: int, cutoff_time: datetime, news_dict: Dict[int, "NewsItem"]
    ) -> np.ndarray:
        """실제 히스토리 임베딩 계산 (사전 정렬된 배열 + bisect로 cutoff 이전 로그만 선택).
        메모이즈 캐시를 우회하는 내부 헬퍼 - 테스트에서 호출 횟수를 스파이하기 위해 분리."""
        news_half_life = self.config['time_decay']['news_half_life_days']
        min_weight = self.config['time_decay']['min_weight']
        hist_emb = np.zeros(1024, dtype=np.float32)

        timestamps, news_ids = self._history_user_logs.get(user_id, ([], []))
        if not timestamps:
            return hist_emb

        # bisect_left: timestamps는 오름차순 정렬되어 있으므로, 반환값은
        # "cutoff_time보다 앞선(< cutoff_time)" 로그의 개수와 같다.
        cut_idx = bisect.bisect_left(timestamps, cutoff_time)
        if cut_idx == 0:
            return hist_emb

        vectors, weights = [], []
        for i in range(cut_idx):
            nid = int(news_ids[i])
            if nid not in news_dict:
                continue
            vec = news_dict[nid].embedding

            days_ago = (cutoff_time - timestamps[i]).days
            w = pow(0.5, days_ago / news_half_life)
            w = max(w, min_weight)

            vectors.append(vec)
            weights.append(w)

        if vectors:
            hist_emb = np.average(vectors, axis=0, weights=weights)
        return hist_emb

    def compute_history_embedding(
        self,
        user_id: int,
        cutoff_time: datetime,
        logs_df: pd.DataFrame,
        news_dict: Dict[int, "NewsItem"],
    ) -> np.ndarray:
        """
        주어진 cutoff_time '이전' 로그만 사용해 point-in-time 히스토리 임베딩을 계산한다.

        Data Leakage 방지: 학습 데이터의 각 row는 그 row가 발생한 시점(cutoff_time) 이후의
        클릭을 히스토리 feature로 사용해서는 안 된다. cutoff_time=datetime.now()로 호출하면
        (실서비스 추론 시나리오) 기존 build_user_profiles()와 동일하게 '현재까지의 전체 히스토리'가 된다.

        성능(CORRECTION): create_inference_dataset()은 (user, news) 전체 조합에 동일한
        eval_timestamp를 넘기므로 timestamps가 항상 채워져 있어, 메모이즈 없이는 O(유저수 x 뉴스수)로
        같은 (user_id, cutoff_time) 히스토리를 반복 계산하게 된다. (user_id, cutoff_time) 단위로
        결과를 캐시해, 실제 계산은 unique (user_id, cutoff_time) 조합당 1회만 수행되도록 한다 -
        추론 시엔 유저당 1회, 학습 시엔 같은 클릭시각을 공유하는 positive+negative 묶음당 1회.
        """
        self._ensure_history_index(logs_df)
        cache_key = (user_id, cutoff_time)
        if cache_key in self._history_embedding_cache:
            return self._history_embedding_cache[cache_key]

        result = self._compute_history_embedding_uncached(user_id, cutoff_time, news_dict)
        self._history_embedding_cache[cache_key] = result
        return result

    def build_user_profiles(self) -> Dict[int, UserProfile]:
        """
        유저 프로필 생성 (실시간 추론 시나리오: cutoff_time = 지금)
        (참고: category_half_life_days 로직은 여기서 제거됨 - Todo 1번 반영)
        """
        if self._user_profiles is not None: return self._user_profiles

        logger.info("👤 사용자 프로필 빌드 중...")

        # 유저 목록 조회 (메타데이터가 있다면)
        user_query = 'SELECT user_id FROM "user"' # user는 예약어
        users_df = self._load_from_db(user_query)

        # 선호 카테고리
        pref_cat_df = self.load_user_preferred_categories()
        user_cat_map = pref_cat_df.groupby('user_id')['category_id'].apply(list).to_dict()

        # 뉴스 임베딩 (히스토리 계산용)
        news_dict = self.load_embedded_news()

        # 로그 (히스토리 계산용 - 전체 로드)
        logs_df = self.load_ctr_logs()

        profiles = {}
        now = datetime.now()

        for _, row in users_df.iterrows():
            uid = int(row['user_id'])

            hist_emb = self.compute_history_embedding(
                user_id=uid, cutoff_time=now, logs_df=logs_df, news_dict=news_dict
            )

            # [수정] category_half_life_days 관련 로직 제거됨.
            # DB에 있는 onboarding_categories를 그대로 사용.

            profiles[uid] = UserProfile(
                user_id=uid,
                # 현재 DB "user" 테이블에 age/gender 컬럼이 명시되지 않아 기본값(0) 처리
                # 필요 시 쿼리에 추가해야 함
                age_band_idx=0, 
                gender_idx=0,
                onboarding_categories=user_cat_map.get(uid, []),
                history_embedding=hist_emb
            )
            
        self._user_profiles = profiles
        logger.info(f"✅ 프로필 생성 완료: {len(profiles)}명")
        return self._user_profiles

    # -------------------------------------------------------------------------
    # 3. Output Handler
    # -------------------------------------------------------------------------

    def save_inference_results(self, results_df: pd.DataFrame):
        """
        추론 결과를 저장 (Debug: CSV / Prod: DB Insert)
        """
        mode = self.config.get('execution_env', 'debug')
        
        # 1. Debug Mode (CSV 저장)
        if mode == 'debug':
            res_dir = self.config['output']['results_dir']
            os.makedirs(res_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(res_dir, f"rec_{timestamp}.csv")
            
            results_df.to_csv(path, index=False)
            logger.info(f"💾 [Debug] 결과 CSV 저장 완료: {path}")
            
        # 2. Production Mode (DB Insert)
        elif mode == 'production':
            logger.info("💾 [Production] DB에 결과 업로드 시작...")
            
            # user_id 별로 news_letter_ids 리스트 묶기
            # 가정: results_df는 ['user_id', 'news_letter_id'] 컬럼을 가짐
            grouped = results_df.groupby('user_id')['news_letter_id'].apply(list).reset_index()
            
            # DB Insert를 위한 데이터 준비
            insert_data = []
            for _, row in grouped.iterrows():
                insert_data.append({
                    'uid': int(row['user_id']),
                    'nids': json.dumps([int(x) for x in row['news_letter_id']]), # JSON array string
                })
                
            if not insert_data:
                logger.warning("⚠️ 저장할 추론 결과가 없습니다.")
                return

            # Bulk Insert Query
            # created_at은 DB의 NOW() 사용
            insert_query = text("""
                INSERT INTO news_letter_today_batch (user_id, news_letter_ids, created_at)
                VALUES (:uid, :nids, NOW())
            """)
            
            try:
                with self.engine.begin() as conn: # Transaction
                    conn.execute(insert_query, insert_data)
                logger.info(f"✅ DB 업로드 완료: {len(insert_data)}명 유저")
            except Exception as e:
                logger.error(f"❌ DB 업로드 실패: {e}")
                raise e

    # -------------------------------------------------------------------------
    # 4. Helpers
    # -------------------------------------------------------------------------
    
    def get_all_news_ids(self) -> List[int]:
        return list(self.load_embedded_news().keys())

    def get_all_user_ids(self) -> List[int]:
        # User 테이블 조회
        query = 'SELECT user_id FROM "user"'
        df = self._load_from_db(query)
        return df['user_id'].tolist()