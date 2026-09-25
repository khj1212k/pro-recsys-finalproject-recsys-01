# Stage0: 사용자 임베딩 생성
# - 선호 뉴스레터 + 클릭 이력 기반 가중 평균
# - 시간 감쇠 적용하여 최신 관심사 반영

import math
import logging
import numpy as np
from datetime import datetime, timedelta
from typing import Dict
from db.connection import get_connection, release_connection

logger = logging.getLogger(__name__)


def _to_vector_array(value) -> "np.ndarray | None":
    """raw-SQL(psycopg2 cursor)로 읽은 vector 컬럼 값을 numpy array로 변환한다.

    db.connection.get_connection()으로 얻은 연결에는 register_pgvector_adapter가
    적용돼 있다. pgvector.psycopg2.register_vector는 vector 컬럼 값을
    numpy.ndarray가 아니라 pgvector.Vector 객체로 돌려주므로, 이를 그대로
    np.array(v)에 넘기면 값이 아니라 Vector 객체 자체를 감싼 0차원 object
    배열이 만들어져 이후 np.stack/곱셈에서 깨진다. extension이 등록되지 않은
    연결(레거시 경로)에서는 여전히 문자열로 오므로 그 경우도 처리한다.
    """
    if value is None:
        return None

    if isinstance(value, np.ndarray):
        return value.astype(np.float32, copy=False)

    # pgvector.Vector 등 to_numpy()를 제공하는 래퍼 (duck-typing)
    if hasattr(value, "to_numpy"):
        return value.to_numpy().astype(np.float32, copy=False)

    if isinstance(value, (list, tuple)):
        return np.array(value, dtype=np.float32)

    if value == "":
        return None

    # 문자열 폴백 (extension 미등록 등으로 register_vector가 적용 안 된 경우)
    clean = str(value).strip().strip("[]{}")
    if not clean:
        return None
    parts = clean.split(",") if "," in clean else clean.split()
    return np.array([float(p) for p in parts if p.strip()], dtype=np.float32)


class UserEmbedder:
    def __init__(self, pref_weight=0.4, decay_rate=0.05, lookback=90, min_inte=1):
        self.P_W = pref_weight
        self.DECAY = decay_rate
        self.LOOKBACK = lookback
        self.MIN_INTE = min_inte

    def batch_update_all_users(self) -> Dict[str, int]:
        """아직 장기 벡터가 없는 사용자만 채운다 (파이프라인 Stage0)."""
        return self._update_users('SELECT user_id FROM "user" WHERE user_embedding IS NULL', ())

    def refresh_recently_active_users(self, since: datetime) -> Dict[str, int]:
        """since 이후 클릭한 사용자의 장기 벡터를 같은 가중식으로 다시 계산한다.

        batch_update_all_users는 NULL인 사용자만 채우므로 한 번 만들어진 벡터는 클릭이
        쌓여도 바뀌지 않았다. 요청 시점 추천(backend/app/recsys, ADR 0015)은 이 벡터를
        장기 선호로 읽으므로 주기적으로 이 함수를 돌려 최근 활동을 반영한다(스케줄링은
        별도 잡). since는 tz-aware datetime을 권장한다 - psycopg2가 timestamptz로 넘겨
        DB 서버 TimeZone 기준으로 비교된다.
        """
        return self._update_users(
            "SELECT DISTINCT user_id FROM user_newsletter_ctr_log WHERE created_at >= %s",
            (since,),
        )

    def _update_users(self, target_sql: str, target_params: tuple) -> Dict[str, int]:
        stats = {'success': 0, 'failed': 0, 'skipped': 0}
        updates = []

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # 1. 대상 사용자 조회
                cur.execute(target_sql, target_params)
                targets = [r[0] for r in cur.fetchall()]
                if not targets: return stats

                # 2. 데이터 일괄 조회
                logger.info(f"Processing {len(targets)} users...")
                t_tuple = tuple(targets) 
                
                # (1) 선호도
                cur.execute(f"SELECT user_id, news_letter_id \
                              FROM user_preferred_newsletter \
                              WHERE user_id IN %s", (t_tuple,)) 
                prefs_map = {}
                for uid, nid in cur.fetchall():
                    prefs_map.setdefault(uid, []).append(nid)

                # (2) 이력
                cutoff = datetime.now() - timedelta(days=self.LOOKBACK)
                cur.execute(f"SELECT user_id, news_letter_id, created_at \
                              FROM user_newsletter_ctr_log \
                              WHERE user_id IN %s AND created_at >= %s", (t_tuple, cutoff))
                hist_map = {} 
                all_nids = set() 
                for uid, nid, dt in cur.fetchall():
                    hist_map.setdefault(uid, []).append((nid, dt))
                    all_nids.add(nid)
                    
                for p in prefs_map.values(): 
                    all_nids.update(p) # 선호 뉴스레터, 이력 뉴스레터 모두 포함

                # (3) 벡터
                vec_map = {} 
                if all_nids:
                    cur.execute("SELECT news_letter_id, news_letter_embedding \
                                FROM news_letter \
                                WHERE news_letter_id = ANY(%s)", (list(all_nids),))
                    vec_map = {}
                    for nid, raw_vec in cur.fetchall():
                        arr = _to_vector_array(raw_vec)
                        if arr is not None:
                            vec_map[nid] = arr

                # 3. 계산
                now = datetime.now()
                for uid in targets: 
                    ps = prefs_map.get(uid, []) 
                    hs = hist_map.get(uid, []) 

                    if len(ps) + len(hs) < self.MIN_INTE:
                        stats['skipped'] += 1; continue

                    # 가중치 계산
                    weights = {nid: self.P_W for nid in ps} # 선호 뉴스레터 가중치(0.4)
                    for nid, dt in hs: # 이력 뉴스레터 가중치
                        days = (now - dt).total_seconds() / 86400 # 날짜 차이 계산
                        # 최종 가중치 = (1 - 선호 가중치) * e^(-감쇠율 * 날짜 차이)
                        weights[nid] = weights.get(nid, 0.0) + (1 - self.P_W) * math.exp(-self.DECAY * days)

                    # 벡터가 있는 뉴스레터만
                    valid_ids = [n for n in weights if n in vec_map]
                    if not valid_ids:
                        stats['skipped'] += 1; continue
                        
                    # 벡터 가중 합
                    vs = np.stack([vec_map[n] for n in valid_ids])
                    ws = np.array([weights[n] for n in valid_ids]).reshape(-1, 1)
                    emb = np.sum(vs * ws, axis=0) / np.sum(ws)
                    
                    # 정규화, :=는 계산 및 변수저장 
                    if (norm := np.linalg.norm(emb)) > 0: # 0으로 나누는거 방지
                        updates.append(((emb / norm).tolist(), uid))
                        stats['success'] += 1
                    else:
                        stats['failed'] += 1

                # 4. 저장
                if updates:
                    cur.executemany('UPDATE "user" \
                                     SET user_embedding=%s \
                                     WHERE user_id=%s', updates)
            
            conn.commit() 

        except Exception as e:
            conn.rollback() 
            logger.error(f"Error: {e}")
            stats = {'success': 0, 
                     'failed': len(targets) if 'targets' in locals() else 0, 
                     'skipped': 0}
        finally:
            release_connection(conn)
            
        return stats
