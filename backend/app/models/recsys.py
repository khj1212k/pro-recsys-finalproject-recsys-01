import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

MODEL_ROLES = ("active", "shadow", "retired")


# 요청 시점 추천이 실제로 화면에 내보낸 목록 - 화면의 칸(slot) 하나가 한 행이다.
# 쓰기 빈도가 높은 append-only 로그라 FK를 걸지 않는다 - 뉴스레터/유저 삭제가
# 로그 때문에 막히거나, INSERT마다 FK 검사 비용을 치르지 않도록.
#
# 컬럼 의미(docs/adr/0015 "노출 로그", docs/adr/0025):
# - position: 화면 응답(표시 단계를 거친 목록) 안의 0부터 시작하는 순위. (request_id, position)은 유일하다.
# - score: realtime/cold_start_onboarding/cold_start_category는 MMR 재정렬 전의 활성 스코어러 점수,
#   cold_start_popular는 인기도-최신성 점수, batch/popular/recent 폴백은 NULL.
# - model_version: "lgbm:<name>@<version>"까지 들어가야 한다(레지스트리의 이름·버전이 각각
#   64자라 최대 134자).
# - explored: 탐색으로 채운 칸이면 true.
# - propensity: 로그 정책에서 "이 아이템이 이 위치에 놓일 확률". 탐색 칸은 (m/S)/|E'|, 결정론 칸은
#   순위가 탐색 칸에 밀려 이 위치로 올 확률(app/recsys/exploration.py의 닫힌 식)이고 탐색이 꺼져 있으면 1이다.
#   NULL은 "이 행으로 off-policy 추정을 하면 안 된다"는 뜻이다: 폴백 응답(batch/popular/recent),
#   화면에 나간 목록이 계획한 목록과 달라진 요청, 로그 v2 이전의 행.
# - det_rank: 결정론 목록에서의 0부터 시작하는 순위. 탐색 칸은 NULL.
# - scores_shadow: {"모델 버전": 점수}. 같은 후보를 shadow 스코어러가 매긴 점수이고 응답 순서에는 쓰이지 않는다.
# - features: 활성 스코어러가 이 아이템에 쓴 피처의 float32 little-endian 바이트. 해석은 요청 로그의
#   feature_schema_version이 정한다(app/recsys/scoring.py FEATURE_SCHEMAS).
class RecommendationImpressionLog(SQLModel, table=True):
    __tablename__ = "recommendation_impression_log"
    __table_args__ = (
        Index(
            "ix_recommendation_impression_log_user_id_created_at",
            "user_id",
            text("created_at DESC"),
        ),
        UniqueConstraint(
            "request_id", "position", name="uq_recommendation_impression_log_request_position"
        ),
    )

    impression_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    request_id: uuid.UUID = Field(sa_column=Column(Uuid, nullable=False))
    user_id: int = Field(nullable=False)
    news_letter_id: int = Field(nullable=False)
    position: int = Field(sa_column=Column(SmallInteger, nullable=False))
    score: Optional[float] = Field(default=None, sa_column=Column(Float, nullable=True))
    source: str = Field(sa_column=Column(String(32), nullable=False))
    model_version: str = Field(sa_column=Column(String(160), nullable=False))
    propensity: Optional[float] = Field(default=None, sa_column=Column(Float, nullable=True))
    explored: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=false()),
    )
    det_rank: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    # none_as_null: shadow 점수가 없는 칸은 JSON null이 아니라 SQL NULL로 남긴다(기본값이면 None이 'null'::jsonb로
    # 들어가 "scores_shadow IS NULL"로 걸러지지 않는다).
    scores_shadow: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB(none_as_null=True), nullable=True)
    )
    features: Optional[bytes] = Field(default=None, sa_column=Column(LargeBinary, nullable=True))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


# 추천 응답 하나가 한 행이다(ADR 0025). 칸 로그와 같은 트랜잭션에서 쓰므로 request_id로 1:N 조인된다.
# 빈 응답(source='empty')도 한 행을 남긴다 - 칸 로그만으로는 빈 응답을 셀 수 없었다.
#
# - source / model_version: 응답 헤더 X-Rec-Source / X-Model-Version과 같은 값.
# - policy_version: 화면을 만든 탐색 정책. 'eps-uniform-v1' | 'deterministic' | 'none'(폴백 응답).
# - profile_source: 사용자 프로필의 출처 long_term | onboarding | category | none. 폴백 응답은 NULL.
# - cache_hit: 결정론 목록을 캐시에서 읽었는지. 탐색 칸은 캐시와 무관하게 요청마다 새로 뽑는다.
# - fallback_reason: timeout | empty | error. 실시간 경로가 답했으면 NULL.
# - candidate_count: 후보 생성기가 낸 합집합 크기(제외 전).
# - candidate_ids / eligible_count: 제외(이미 클릭, 피로 규칙 enforce)와 표시 가능 필터를 거친 후보 E와
#   그 크기. 탐색이 뽑는 모집단이고, 풀 네거티브 표집과 replay가 조건으로 삼는 집합이다.
# - explore_pool_size: |E'| = E에서 결정론 칸에 들어간 아이템을 뺀 수.
# - slate_size / shown_count: 계획한 화면 칸 수 / 표시 단계를 거쳐 실제로 나간 칸 수. 둘이 다르면
#   그 요청의 칸 로그 propensity는 NULL이다.
# - explore_positions: 탐색 칸의 위치(오름차순). 탐색이 없으면 빈 배열, 폴백 응답은 NULL.
# - feature_schema_version: 칸 로그 features의 해석 버전. 피처를 남기지 않았으면 NULL.
# - shadow_versions: 이 요청의 후보에 점수를 매긴 shadow 모델 버전들.
# - fatigue_mode / fatigued_count: 노출 피로 규칙의 모드(off | log | enforce)와, 규칙에 걸린 후보 수.
#   log 모드에서는 세기만 하고 후보에서 빼지 않는다.
# - latency_ms: 서비스가 추천을 만드는 데 쓴 시간(폴백 포함, 표시 단계와 직렬화는 제외).
class RecommendationRequestLog(SQLModel, table=True):
    __tablename__ = "recommendation_request_log"
    __table_args__ = (
        Index(
            "ix_recommendation_request_log_user_id_created_at",
            "user_id",
            text("created_at DESC"),
        ),
    )

    request_id: uuid.UUID = Field(sa_column=Column(Uuid, primary_key=True))
    user_id: int = Field(nullable=False)
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    source: str = Field(sa_column=Column(String(32), nullable=False))
    model_version: str = Field(sa_column=Column(String(160), nullable=False))
    policy_version: str = Field(sa_column=Column(String(32), nullable=False))
    profile_source: Optional[str] = Field(default=None, sa_column=Column(String(16), nullable=True))
    cache_hit: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=false()),
    )
    fallback_reason: Optional[str] = Field(default=None, sa_column=Column(String(16), nullable=True))
    candidate_count: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    eligible_count: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    explore_pool_size: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    slate_size: int = Field(sa_column=Column(SmallInteger, nullable=False))
    shown_count: int = Field(sa_column=Column(SmallInteger, nullable=False))
    explore_positions: Optional[list] = Field(
        default=None, sa_column=Column(ARRAY(SmallInteger), nullable=True)
    )
    candidate_ids: Optional[list] = Field(default=None, sa_column=Column(ARRAY(Integer), nullable=True))
    feature_schema_version: Optional[int] = Field(
        default=None, sa_column=Column(SmallInteger, nullable=True)
    )
    shadow_versions: Optional[list] = Field(
        default=None, sa_column=Column(ARRAY(String(160)), nullable=True)
    )
    fatigue_mode: Optional[str] = Field(default=None, sa_column=Column(String(8), nullable=True))
    fatigued_count: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    latency_ms: Optional[int] = Field(default=None, sa_column=Column(Integer, nullable=True))


# 요청 시점 랭커(LightGBMScorer)가 읽는 모델 저장소. API 프로세스가 여러 개여도
# 공유 파일시스템 없이 같은 모델을 읽을 수 있도록 LightGBM text 모델 본문을
# 그대로 저장한다. 이름별로 활성 모델은 최대 1개(부분 UNIQUE 인덱스).
#
# role(ADR 0025): 'active'는 목록을 만드는 모델, 'shadow'는 같은 후보에 점수만 매겨 로그에 남기는 모델
# (여러 개 가능, 서빙은 최신 RECSYS_SHADOW_MAX개만 읽는다), 'retired'는 읽지 않는 모델이다. 새 행의
# 기본값은 'shadow'다 - 등록만으로 응답이 바뀌지 않게. is_active는 role = 'active'와 항상 같다(체크 제약).
class ModelRegistry(SQLModel, table=True):
    __tablename__ = "model_registry"
    __table_args__ = (
        UniqueConstraint("model_name", "model_version", name="uq_model_registry_name_version"),
        Index(
            "uq_model_registry_one_active_per_name",
            "model_name",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("role IN ('active', 'shadow', 'retired')", name="ck_model_registry_role"),
        CheckConstraint("is_active = (role = 'active')", name="ck_model_registry_active_matches_role"),
    )

    model_id: Optional[int] = Field(default=None, primary_key=True)
    model_name: str = Field(sa_column=Column(String(64), nullable=False))
    model_version: str = Field(sa_column=Column(String(64), nullable=False))
    model_format: str = Field(
        default="lightgbm_text",
        sa_column=Column(String(32), nullable=False, server_default="lightgbm_text"),
    )
    model_text: str = Field(sa_column=Column(Text, nullable=False))
    feature_names: Optional[list] = Field(default=None, sa_column=Column(JSON))
    metrics: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    is_active: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=false()),
    )
    role: str = Field(
        default="shadow",
        sa_column=Column(String(16), nullable=False, server_default="shadow"),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
