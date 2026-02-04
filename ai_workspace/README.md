# AI News Pipeline

**LangGraph 기반 뉴스 클러스터링 및 뉴스레터 자동 생성 파이프라인**

뉴스 RSS 피드를 수집하여 유사 기사를 클러스터링하고, LLM을 활용해 뉴스레터를 자동 생성하는 End-to-End 파이프라인입니다.

---

## Overview

이 프로젝트는 다음과 같은 문제를 해결합니다:

- **정보 과부하**: 수십 개 언론사에서 쏟아지는 뉴스를 주제별로 자동 분류
- **중복 제거**: 같은 사건을 다룬 여러 기사를 하나의 뉴스레터로 통합
- **개인화**: 사용자 클릭 이력 기반 임베딩으로 맞춤형 추천 지원

```
[RSS 수집] → [본문 추출] → [임베딩] → [클러스터링] → [뉴스레터 생성] → [추천 시스템]
```

---

## Key Features

### 1. 멀티 스테이지 파이프라인
- 6단계로 구성된 모듈화된 파이프라인
- 각 단계별 독립 실행 가능 (`--from-stage`, `--to-stage`)

### 2. LangGraph 기반 워크플로우
- 상태 기반 그래프로 뉴스레터 생성 흐름 제어
- LLM 평가 → 재생성 → 품질 통과 시 저장의 반복 루프
- 클러스터 품질 평가 및 아웃라이어 자동 제거

### 3. 임베딩 & 클러스터링
- BGE-M3 모델로 1024차원 벡터 생성
- HDBSCAN으로 밀도 기반 클러스터링
- 2차 분할 로직으로 혼합 주제 클러스터 분리

### 4. 품질 보증 시스템
- 클러스터 일관성 평가 (주제 통일성 검증)
- 뉴스레터 품질 평가 (팩트 체크, 구조 검증)
- 문체 변환 (딱딱한 문체 → 친근한 대화체)

---

## Tech Stack

| 분류 | 기술 |
|------|------|
| **Language** | Python 3.10+ |
| **Workflow** | LangGraph |
| **Embedding** | BGE-M3 (BAAI/bge-m3) |
| **Clustering** | HDBSCAN, K-Means |
| **LLM** | Naver HyperCLOVA X, OpenAI GPT |
| **NLP** | Kiwipiepy (한국어 형태소 분석) |
| **Database** | PostgreSQL + pgvector |
| **Crawling** | Trafilatura, Feedparser |
| **Scheduler** | Apache Airflow |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AI News Pipeline                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────────────┐ │
│   │  Stage0  │    │  Stage1  │    │  Stage2  │    │       Stage3         │ │
│   │   User   │    │   RSS    │    │ Content  │    │      Article         │ │
│   │Embedding │    │Collection│    │Extraction│    │      Embedding       │ │
│   └────┬─────┘    └────┬─────┘    └────┬─────┘    └──────────┬───────────┘ │
│        │               │               │                     │             │
│        ▼               ▼               ▼                     ▼             │
│   ┌─────────┐    ┌─────────┐    ┌─────────┐          ┌─────────────┐      │
│   │  user   │    │news_raw │    │news_raw │          │  news_raw   │      │
│   │embedding│    │ (meta)  │    │(content)│          │ (embedding) │      │
│   └─────────┘    └─────────┘    └─────────┘          └──────┬──────┘      │
│                                                              │             │
│   ┌──────────────────────────────────────────────────────────┘             │
│   │                                                                        │
│   ▼                                                                        │
│   ┌────────────────────────────────────────────────────────────────────┐   │
│   │                    Stage5: Newsletter Generation                   │   │
│   │  ┌─────────────────────────────────────────────────────────────┐   │   │
│   │  │                   LangGraph Workflow                        │   │   │
│   │  │                                                             │   │   │
│   │  │  ┌──────────┐    ┌──────────┐    ┌──────────┐              │   │   │
│   │  │  │ HDBSCAN  │───▶│ Cluster  │───▶│Newsletter│              │   │   │
│   │  │  │Clustering│    │   Eval   │    │   Gen    │              │   │   │
│   │  │  └──────────┘    └────┬─────┘    └────┬─────┘              │   │   │
│   │  │                       │               │                     │   │   │
│   │  │                  PASS/FAIL        ┌───┴───┐                 │   │   │
│   │  │                       │           ▼       │                 │   │   │
│   │  │                       │      ┌────────┐   │ RETRY           │   │   │
│   │  │                       │      │  Eval  │───┘                 │   │   │
│   │  │                       │      │Newsletter                    │   │   │
│   │  │                       │      └───┬────┘                     │   │   │
│   │  │                       │          │ PASS                     │   │   │
│   │  │                       │          ▼                          │   │   │
│   │  │                       │    ┌───────────┐    ┌──────────┐   │   │   │
│   │  │                       │    │  Embed &  │───▶│   Save   │   │   │   │
│   │  │                       │    │  Convert  │    │   to DB  │   │   │   │
│   │  │                       │    └───────────┘    └──────────┘   │   │   │
│   │  └─────────────────────────────────────────────────────────────┘   │   │
│   └────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
ai_workspace/
├── main.py                     # 파이프라인 진입점 (CLI)
├── config/
│   └── settings.py             # 환경별 설정 (dev/staging/prod)
│
├── pipeline/                   # 파이프라인 오케스트레이션
│   ├── runner.py               # 전체 실행 흐름 관리
│   └── stages.py               # Stage 0~5 정의
│
├── crawler/                    # 데이터 수집 레이어
│   ├── rss_collector.py        # RSS 피드 수집
│   ├── embedding_generator.py  # 배치 임베딩 생성
│   └── content_extractor/      # 본문 추출
│       ├── extractor.py        # Trafilatura 기반 추출
│       ├── cleaners.py         # 노이즈 제거
│       └── patterns.py         # 언론사별 정제 패턴
│
├── core/                       # 핵심 비즈니스 로직
│   ├── embedder.py             # BGE-M3 임베딩 모델
│   ├── user_embedder.py        # 사용자 임베딩 (추천용)
│   ├── llm_client.py           # LLM API 클라이언트
│   ├── tone_converter.py       # 문체 변환기
│   │
│   ├── clustering/             # 클러스터링 모듈
│   │   ├── hdbscan_clusterer.py    # HDBSCAN 기반 클러스터링
│   │   └── split_v2.py             # 클러스터 분할 판단
│   │
│   └── reconstruction/         # 뉴스레터 재구성
│       ├── generator.py        # 2-call 방식 본문 생성
│       ├── prompts.py          # LLM 프롬프트 템플릿
│       ├── validator.py        # 결과 검증/정제
│       └── repository.py       # DB 저장 로직
│
├── workflow/                   # LangGraph 워크플로우
│   ├── graph.py                # 그래프 정의 (Node + Edge)
│   ├── nodes.py                # 노드 함수들
│   ├── state.py                # State 스키마
│   ├── evaluators.py           # LLM 기반 평가기
│   └── helpers.py              # 헬퍼 함수
│
├── db/                         # 데이터베이스 레이어
│   ├── connection.py           # PostgreSQL 연결 관리
│   ├── schema.py               # 테이블 스키마
│   ├── batch_manager.py        # 배치 저장 로직
│   └── user_log_queries.py     # 사용자 로그 쿼리
│
├── utils/                      # 공통 유틸리티
│   ├── logger.py               # 로깅 설정
│   └── exceptions.py           # 커스텀 예외
│
├── scripts/                    # 유틸리티 스크립트
│   ├── migrate_db_1024.py      # 임베딩 차원 마이그레이션
│   └── update_user_embeddings.py   # 사용자 임베딩 수동 업데이트
│
└── tests/                      # 테스트
    ├── test_workflow.py
    ├── test_clusterer.py
    ├── test_evaluators.py
    └── test_reconstructor.py
```

---

## Pipeline Flow

### Stage 0: User Embedding
사용자의 선호 뉴스레터와 클릭 이력을 기반으로 사용자 임베딩 벡터 생성
- 시간 감쇠 적용 (최신 관심사에 높은 가중치)
- 선호 뉴스레터 + 클릭 이력의 가중 평균

### Stage 1: RSS Collection
10개 이상의 언론사 RSS 피드에서 뉴스 메타데이터 수집
- 중복 URL 자동 필터링
- 동아일보, 경향신문, 매일경제, 한국경제, 전자신문, AI타임스 등

### Stage 2: Content Extraction
수집된 URL에서 실제 기사 본문 추출
- Trafilatura 기반 HTML 파싱
- 멀티프로세싱 병렬 처리 (8 workers)
- 언론사별 노이즈 패턴 제거 (광고, 기자 서명 등)

### Stage 3: Article Embedding
기사 제목+본문을 BGE-M3 모델로 1024차원 벡터 변환
- GPU 자동 감지 및 활용
- 배치 처리로 효율성 극대화

### Stage 5: Newsletter Generation (LangGraph Workflow)

```
[클러스터링] → [클러스터 평가] → [뉴스레터 생성] → [품질 평가] → [임베딩] → [문체 변환] → [저장]
```

1. **HDBSCAN 클러스터링**: 임베딩 유사도 기반 기사 그룹화
2. **클러스터 평가**: LLM이 주제 일관성 검증, 아웃라이어 제거
3. **뉴스레터 생성**: 2-call 방식 (본문 생성 → 메타데이터 생성)
4. **품질 평가**: 팩트 체크, 구조 검증 (실패 시 재생성)
5. **임베딩 생성**: 원본 텍스트 기반 추천용 벡터 생성
6. **문체 변환**: 딱딱한 문체 → 친근한 대화체 + 이모지
7. **DB 저장**: 변환된 텍스트 + 원본 임베딩 저장

---

## LangGraph Workflow Detail

뉴스레터 생성의 핵심인 LangGraph 워크플로우는 상태 기반 그래프로 구현되어 있습니다.

```python
# workflow/graph.py
workflow = StateGraph(AgentState)

# 노드 정의
workflow.add_node("init_cluster", initialize_cluster_processing)
workflow.add_node("eval_cluster", evaluate_cluster)
workflow.add_node("generate_newsletter", generate_newsletter)
workflow.add_node("eval_newsletter", evaluate_newsletter)
workflow.add_node("embed_newsletter", embed_newsletter_node)
workflow.add_node("convert_tone", convert_tone_node)
workflow.add_node("save_newsletter", save_newsletter_to_db)

# 조건부 분기
workflow.add_conditional_edges(
    "eval_newsletter",
    route_after_newsletter_eval,
    {"pass": "embed_newsletter", "retry": "generate_newsletter", "max_retries": "handle_max_retries"}
)
```

### State Schema
```python
class AgentState(TypedDict):
    # 클러스터 정보
    current_cluster_id: int
    current_articles: List[Dict]

    # 평가 결과
    cluster_eval: Optional[ClusterEvaluation]
    newsletter_eval: Optional[NewsletterEvaluation]

    # 생성 결과
    newsletter_draft: Optional[Dict]
    newsletter_embedding: Optional[List[float]]
    converted_newsletter: Optional[Dict]

    # 재시도 카운터
    newsletter_retry_count: int
```

---

## Clustering Strategy

### 1차: HDBSCAN
- 밀도 기반 클러스터링으로 노이즈 자동 제거
- `min_cluster_size=3`, `min_samples=2`

### 2차: Split 판단
여러 주제가 섞인 클러스터를 감지하고 분할:

```python
# core/clustering/split_v2.py
def decide_split_v2(X: np.ndarray, titles: List[str]) -> SplitDecision:
    # 1. K-Means로 2개 그룹 분할
    # 2. 제목 키워드 Jaccard 유사도 계산
    # 3. Silhouette 점수로 분리도 측정
    # 4. 점수 기반 분할 여부 결정
```

**분할 판단 기준**:
- Jaccard 유사도 < 0.12 → 강제 분할
- Jaccard 유사도 > 0.55 → 분할 거부
- Silhouette + 거리 비율 + 키워드 유사도 종합 점수

---

## Usage

### 기본 실행
```bash
# 전체 파이프라인 실행 (Stage 1~5)
python main.py

# 특정 스테이지만 실행
python main.py --from-stage 3 --to-stage 5

# 사용자 임베딩부터 시작
python main.py --from-stage 0
```

### 옵션
```bash
python main.py \
    --workers 8 \              # 크롤링 병렬 워커 수
    --batch-size 32 \          # 임베딩 배치 크기
    --min-cluster-size 3 \     # HDBSCAN 최소 클러스터 크기
    --min-samples 2 \          # HDBSCAN min_samples
    --limit 10 \               # 처리할 클러스터 수 제한
    --force-cpu                # GPU 대신 CPU 사용
```

---

## Configuration

### 환경 변수
```bash
# .env
ENV=dev                          # dev / staging / prod

# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=news_db
DB_USER=postgres
DB_PASSWORD=password

# LLM
LLM_PROVIDER=naver               # naver / openai
NCP_CLOVASTUDIO_API_KEY=xxx
NCP_APIGW_API_KEY=xxx
OPENAI_API_KEY=xxx
```

### 환경별 설정
```python
# config/settings.py
class DevSettings(BaseSettings):
    LOG_LEVEL = "DEBUG"
    DB_POOL_MAX = 5

class ProdSettings(BaseSettings):
    LOG_LEVEL = "WARNING"
    DB_POOL_MAX = 20
    PARALLEL_WORKERS = 8
```

---

## Database Schema

### 주요 테이블

| 테이블 | 설명 |
|--------|------|
| `news_raw` | 원본 기사 (제목, 본문, 임베딩) |
| `news_letter` | 생성된 뉴스레터 |
| `user_newsletter_ctr_log` | 사용자 클릭 이력 |
| `users` | 사용자 정보 및 임베딩 |

### 벡터 검색
```sql
-- pgvector를 활용한 유사 뉴스레터 검색
SELECT * FROM news_letter
ORDER BY news_letter_embedding <=> query_vector
LIMIT 10;
```

---

## Performance

| 단계 | 처리량 | 비고 |
|------|--------|------|
| RSS 수집 | ~500건/분 | 10개 피드 기준 |
| 본문 추출 | ~100건/분 | 8 workers |
| 임베딩 생성 | ~50건/분 | GPU, batch=32 |
| 뉴스레터 생성 | ~2건/분 | LLM API 의존 |

---

## Testing

```bash
# 전체 테스트
pytest tests/

# 특정 모듈 테스트
pytest tests/test_workflow.py -v
pytest tests/test_clusterer.py -v
```

---

## Troubleshooting

### GPU 메모리 부족 (OOM)
```bash
# 배치 크기 줄이기
python main.py --batch-size 8

# CPU 모드로 전환
python main.py --force-cpu
```

### LLM Rate Limit (429 에러)
- 자동 지수 백오프 적용됨
- `config/settings.py`에서 재시도 설정 조정 가능

### 데이터베이스 연결 실패
- `.env` 파일의 DB 설정 확인
- PostgreSQL 서버 상태 확인
- `pgvector` 확장 설치 여부 확인

---

## Future Improvements

- [ ] 실시간 스트리밍 처리 (Kafka 연동)
- [ ] 멀티 언어 지원 (영문 뉴스)
- [ ] A/B 테스트 프레임워크
- [ ] 뉴스레터 퀄리티 대시보드

---

## License

This project is for educational and portfolio purposes.
