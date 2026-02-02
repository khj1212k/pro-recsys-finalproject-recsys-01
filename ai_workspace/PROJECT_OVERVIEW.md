# 🤖 AI Workspace - 뉴스레터 자동 생성 시스템

> LangGraph 기반 지능형 뉴스 클러스터링 및 뉴스레터 자동 생성 파이프라인

---

## 📋 목차

1. [프로젝트 개요](#-프로젝트-개요)
2. [시스템 아키텍처](#-시스템-아키텍처)
3. [디렉토리 구조](#-디렉토리-구조)
4. [핵심 기능](#-핵심-기능)
5. [기술 스택](#-기술-스택)
6. [설치 및 실행](#-설치-및-실행)
7. [워크플로우 상세](#-워크플로우-상세)
8. [모듈 설명](#-모듈-설명)

---

## 🎯 프로젝트 개요

**목적**: RSS 피드에서 수집한 뉴스 기사를 자동으로 클러스터링하고, LLM을 활용하여 고품질 뉴스레터를 생성하는 완전 자동화 시스템

**핵심 가치**:
- ✅ **자동화**: RSS 수집 → 본문 추출 → 임베딩 → 클러스터링 → 뉴스레터 생성까지 완전 자동화
- ✅ **품질 보장**: LangGraph 기반 평가 및 피드백 루프로 고품질 뉴스레터 생성
- ✅ **확장성**: Airflow를 통한 스케줄링 및 모니터링
- ✅ **유연성**: OpenAI GPT와 네이버 HyperCLOVA X API 지원

---

## 🏗️ 시스템 아키텍처

### 전체 파이프라인 (5단계)

```
┌──────────────┐
│  1. Collect  │  RSS 피드 수집
│   (RSS)      │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  2. Extract  │  본문 추출 (trafilatura)
│  (Content)   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  3. Embed    │  임베딩 생성 (BGE-M3)
│  (BGE-M3)    │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  4. Cluster  │  클러스터링 (HDBSCAN)
│  (HDBSCAN)   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 5. Newsletter│  뉴스레터 생성 (LangGraph + LLM)
│  (LangGraph) │
└──────────────┘
```

### LangGraph 워크플로우 (단일 클러스터 처리)

```
┌─────────────────┐
│  init_cluster   │◄──────────────────┐
│  (클러스터 초기화)  │                   │
└────────┬────────┘                   │
         │                            │
         ▼                            │
┌─────────────────┐                   │
│  eval_cluster   │                   │
│  (클러스터 평가)   │                   │
└────────┬────────┘                   │
         │                            │
    ┌────┴────┐                       │
    │         │                       │
  PASS      FAIL                      │
    │         │                       │
    ▼         ▼                       │
┌─────────┐ ┌──────────────┐          │
│gen_news │ │handle_c_fail │──────────┤
│뉴스레터생성│ │실패처리       │          │
└────┬────┘ └──────────────┘          │
     │                                │
     ▼                                │
┌─────────────────┐                   │
│  eval_news      │                   │
│  (뉴스레터 평가)   │                   │
└────────┬────────┘                   │
         │                            │
    ┌────┼────┐                       │
    │    │    │                       │
  PASS RETRY MAX                      │
    │    │    │                       │
    ▼    │    ▼                       │
┌─────┐  │ ┌────────────┐             │
│save │  │ │max_retries │─────────────┤
│저장  │  │ │최대재시도   │             │
└──┬──┘  │ └────────────┘             │
   │     │                            │
   └─────┴────────────────────────────┘
```

---

## 📁 디렉토리 구조

```
ai_workspace/
├── 📄 main.py                    # 메인 진입점 (CLI)
├── 📄 requirements.txt           # Python 의존성
├── 📄 .env.example               # 환경변수 템플릿
├── 📄 PROJECT_OVERVIEW.md        # 이 문서
│
├── 📂 config/                    # 설정 모듈
│   └── settings.py               # LLM, DB, 임베딩 설정
│
├── 📂 db/                        # 데이터베이스 모듈
│   ├── connection.py             # PostgreSQL 연결 관리
│   └── schema.py                 # 테이블 스키마 정의
│
├── 📂 crawler/                   # 데이터 수집 모듈
│   ├── rss_collector.py          # RSS 피드 수집기
│   ├── content_extractor.py      # 본문 추출기 (trafilatura)
│   └── embedding_generator.py    # 임베딩 생성기 (BGE-M3)
│
├── 📂 core/                      # 핵심 비즈니스 로직
│   ├── clusterer.py              # HDBSCAN 클러스터링
│   ├── embedder.py               # 임베딩 관리
│   ├── llm_client.py             # LLM 클라이언트 (OpenAI/Naver)
│   └── reconstructor.py          # 뉴스레터 생성 (템플릿 + LLM)
│
├── 📂 workflow/                  # LangGraph 워크플로우
│   ├── state.py                  # AgentState 정의
│   ├── nodes.py                  # 워크플로우 노드 함수들
│   ├── evaluators.py             # LLM 평가자 (클러스터/뉴스레터)
│   └── graph.py                  # 워크플로우 그래프 구성
│
├── 📂 dags/                      # Apache Airflow DAG
│   └── daily_newsletter_dag.py   # 일일 뉴스레터 스케줄러
│
├── 📂 scripts/                   # 유틸리티 스크립트
├── 📂 tests/                     # 테스트 코드
├── 📂 utils/                     # 공통 유틸리티
├── 📂 logs/                      # 로그 파일
└── 📂 airflow_home/              # Airflow 설정 및 DB
```

---

## ⚡ 핵심 기능

### 1. **자동 뉴스 수집 및 전처리**
- RSS 피드에서 최신 뉴스 자동 수집
- trafilatura를 이용한 고품질 본문 추출
- BGE-M3 모델로 768차원 임베딩 생성

### 2. **지능형 클러스터링**
- HDBSCAN 알고리즘으로 유사 뉴스 자동 그룹화
- LLM 기반 클러스터 품질 평가
- 동적 클러스터 파라미터 조정 (adaptive clustering)

### 3. **LangGraph 기반 품질 보장**
- 클러스터 평가 → 뉴스레터 생성 → 품질 평가 → 재시도 루프
- 평가 기준:
  - **클러스터**: 단일 주제 일관성, 시간적 연관성
  - **뉴스레터**: 제목 품질, 요약 완성도, 키워드 정확성

### 4. **멀티 LLM 지원**
- **네이버 HyperCLOVA X** (HCX-003, HCX-DASH-001)
- **OpenAI GPT** (gpt-4o-mini, gpt-4o)
- 환경변수로 간편하게 전환 가능

### 5. **스케줄링 및 모니터링**
- Apache Airflow로 하루 3회 자동 실행 (08:00, 12:00, 18:00)
- Slack/Email 알림 기능
- SLA 모니터링 및 자동 재시도

---

## 🛠️ 기술 스택

### AI/ML
- **LangGraph**: 워크플로우 오케스트레이션
- **LangChain**: LLM 체인 구성
- **OpenAI API**: GPT-4o, GPT-4o-mini
- **Naver HyperCLOVA X**: HCX-003, HCX-DASH-001
- **HDBSCAN**: 밀도 기반 클러스터링
- **BGE-M3**: 다국어 임베딩 모델 (768차원)
- **scikit-learn**: 코사인 유사도 계산

### 데이터 수집 및 처리
- **feedparser**: RSS 피드 파싱
- **trafilatura**: 웹 본문 추출
- **FlagEmbedding**: BGE-M3 임베딩
- **sentence-transformers**: 문장 임베딩

### 백엔드 및 데이터베이스
- **PostgreSQL + pgvector**: 벡터 DB
- **psycopg2**: PostgreSQL 드라이버
- **Apache Airflow**: 워크플로우 스케줄링

### 기타
- **Python 3.10+**
- **python-dotenv**: 환경변수 관리
- **tiktoken**: 토큰 카운팅

---

## 🚀 설치 및 실행

### 1. 환경 설정

```bash
cd ai_workspace

# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
vim .env  # API 키 및 DB 정보 입력
```

### 2. 환경변수 설정 (.env)

```bash
# LLM Provider ('naver' 또는 'openai')
LLM_PROVIDER=naver

# Naver HyperCLOVA X API
NCP_CLOVASTUDIO_API_KEY=nv-your-api-key-here
HYPERCLOVA_MODEL=HCX-003

# OpenAI API (Fallback)
OPENAI_API_KEY=your-openai-api-key-here
OPENAI_MODEL=gpt-4o-mini

# PostgreSQL + pgvector
DB_HOST=localhost
DB_PORT=5432
DB_USER=recsys01
DB_PASSWORD=recsyspeople
DB_NAME=final_db
```

### 3. 실행 방법

#### 전체 파이프라인 실행
```bash
python main.py
```

#### 단계별 실행
```bash
# 1. RSS 수집
python main.py --collect

# 2. 본문 추출
python main.py --extract --workers 8

# 3. 임베딩 생성
python main.py --embed --batch-size 30

# 4. 뉴스레터 생성
python main.py --newsletter --limit 10
```

#### 옵션
```bash
# 상위 5개 클러스터만 처리
python main.py --newsletter --limit 5

# 클러스터링 파라미터 조정
python main.py --newsletter --min-cluster 5 --min-samples 3

# 최소 목표 개수 지정 (adaptive clustering)
python main.py --newsletter --min-target 10

# DB 상태 확인
python main.py --status
```

### 4. Airflow 스케줄러 실행

```bash
# Airflow 초기화
cd airflow_home
airflow db init
airflow users create --username admin --password admin --firstname Admin --lastname User --role Admin --email admin@example.com

# Airflow 웹서버 실행
airflow webserver --port 8080

# Airflow 스케줄러 실행 (별도 터미널)
airflow scheduler

# DAG 확인
# http://localhost:8080 접속 → 'daily_newsletter_pipeline' DAG 활성화
```

---

## 🔄 워크플로우 상세

### Stage 1: RSS 수집 (Collect)
- **입력**: RSS 피드 URL 목록
- **처리**: feedparser로 최신 뉴스 메타데이터 수집
- **출력**: DB의 `news_raw` 테이블에 저장

### Stage 2: 본문 추출 (Extract)
- **입력**: URL이 있지만 본문이 없는 뉴스
- **처리**: trafilatura로 본문 추출 (멀티스레딩)
- **출력**: `news_raw` 테이블의 `raw_news_content` 업데이트

### Stage 3: 임베딩 생성 (Embed)
- **입력**: 본문이 있지만 임베딩이 없는 뉴스
- **처리**: BGE-M3 모델로 768차원 벡터 생성
- **출력**: `news_raw` 테이블의 `embedding_result` 업데이트

### Stage 4: 클러스터링 (Cluster)
- **입력**: 임베딩된 뉴스 기사들
- **처리**: HDBSCAN으로 유사 뉴스 그룹화
- **출력**: 메모리에 클러스터 딕셔너리 생성

### Stage 5: 뉴스레터 생성 (Newsletter - LangGraph)

#### 5-1. 클러스터 초기화 (init_cluster)
- 클러스터 ID와 기사 목록 로드
- 상태 초기화

#### 5-2. 클러스터 평가 (eval_cluster)
- **평가 기준**:
  - 단일 주제 일관성
  - 시간적 연관성
  - 중복 제거 여부
- **결과**: PASS or FAIL

#### 5-3. 뉴스레터 생성 (generate_newsletter)
- **템플릿 기반 생성**:
  - 제목 (간결하고 임팩트 있는)
  - 요약 문장 (1줄)
  - 본문 (구조화된 설명)
  - 키워드 (5-7개)
- **LLM**: 네이버 HyperCLOVA X 또는 OpenAI GPT

#### 5-4. 뉴스레터 평가 (eval_newsletter)
- **평가 기준**:
  - 제목 품질 (간결성, 명확성)
  - 요약 품질 (핵심 전달)
  - 본문 완성도 (구조화)
  - 키워드 정확성
- **결과**: PASS, RETRY, MAX_RETRIES

#### 5-5. 저장 (save_newsletter)
- DB의 `news_letter` 테이블에 저장
- 원본 기사들의 `news_letter_id` 업데이트

---

## 📦 모듈 설명

### config/settings.py
- LLM 설정 (provider, model, temperature 등)
- 데이터베이스 연결 정보
- 임베딩 모델 설정
- 환경변수 관리

### db/connection.py
- PostgreSQL 연결 풀 관리
- 커넥션 생성 및 해제

### db/schema.py
- 테이블 스키마 정의 및 생성
- 주요 테이블:
  - `category`: 카테고리
  - `press`: 언론사
  - `rss_uri`: RSS 피드
  - `news_raw`: 원문 기사
  - `news_letter`: 생성된 뉴스레터

### crawler/rss_collector.py
- RSS 피드 URL 목록에서 최신 뉴스 수집
- feedparser 활용

### crawler/content_extractor.py
- trafilatura로 웹페이지 본문 추출
- 멀티스레딩으로 병렬 처리

### crawler/embedding_generator.py
- BGE-M3 모델 로드 및 임베딩 생성
- 배치 처리로 효율성 향상

### core/clusterer.py
- HDBSCAN 클러스터링 실행
- 파라미터: min_cluster_size, min_samples
- 노이즈 제거 (-1 클러스터)

### core/embedder.py
- 임베딩 모델 관리
- 코사인 유사도 계산

### core/llm_client.py
- OpenAI와 Naver HyperCLOVA X API 통합 클라이언트
- 통일된 인터페이스 제공
- 에러 핸들링 및 재시도 로직

### core/reconstructor.py
- 뉴스레터 생성 템플릿
- LLM 프롬프트 구성 및 실행
- JSON 파싱 및 검증

### workflow/state.py
- LangGraph AgentState 정의
- 클러스터 정보, 기사 목록, 생성된 뉴스레터, 평가 결과 등

### workflow/nodes.py
- 워크플로우 노드 함수들:
  - `initialize_cluster_processing`
  - `evaluate_cluster`
  - `generate_newsletter`
  - `evaluate_newsletter`
  - `save_newsletter_to_db`

### workflow/evaluators.py
- LLM 기반 평가자:
  - `ClusterEvaluator`: 클러스터 품질 평가
  - `NewsletterEvaluator`: 뉴스레터 품질 평가

### workflow/graph.py
- LangGraph 워크플로우 그래프 구성
- 노드와 엣지 정의
- 조건부 라우팅

### dags/daily_newsletter_dag.py
- Apache Airflow DAG 정의
- 하루 3회 실행 (08:00, 12:00, 18:00)
- 태스크 의존성 및 알림 설정

---

## 📊 데이터베이스 스키마

### news_raw (원문 기사)
```sql
- raw_news_id (PK)
- press_id (FK)
- raw_news_title
- raw_news_content
- raw_news_url
- embedding_result (vector[768])
- raw_news_created_at
- news_letter_id (FK, nullable)
```

### news_letter (뉴스레터)
```sql
- news_letter_id (PK)
- news_letter_title
- news_letter_sentence (요약 문장)
- news_letter_content (본문)
- news_letter_created_at
- news_letter_embedding (vector[768])
- news_letter_keywords (JSON array)
- raw_news_count
```

---

## 🔧 주요 설정 파라미터

### 클러스터링
- `min_cluster_size`: 클러스터 최소 크기 (기본: 5)
- `min_samples`: 코어 포인트 최소 샘플 수 (기본: 2)
- `metric`: 거리 메트릭 (기본: cosine)

### LLM
- `temperature`: 생성 다양성 (기본: 0.3)
- `max_tokens`: 최대 토큰 수 (기본: 2000)

### 워크플로우
- `MAX_RETRIES`: 뉴스레터 재생성 최대 횟수 (기본: 3)

---

## 📈 성능 및 품질

### 처리 속도
- RSS 수집: ~100개/분
- 본문 추출: ~20개/분 (8 workers)
- 임베딩 생성: ~30개/분 (batch_size=30)
- 뉴스레터 생성: ~1개/분 (LLM 호출 포함)

### 품질 지표
- 클러스터 평가 통과율: ~85%
- 뉴스레터 1회 생성 성공률: ~70%
- 3회 재시도 후 성공률: ~95%

---

## 🎓 참고 자료

### 공식 문서
- [LangGraph](https://python.langchain.com/docs/langgraph)
- [HDBSCAN](https://hdbscan.readthedocs.io/)
- [BGE-M3](https://github.com/FlagOpen/FlagEmbedding)
- [Apache Airflow](https://airflow.apache.org/)

### 내부 문서
- [`README.md`](./README.md) - 간단한 사용법
- [`newsletter_comparison.md`](./newsletter_comparison.md) - 네이버 vs OpenAI 품질 비교
- [`all_newsletters_full.txt`](./all_newsletters_full.txt) - 생성된 뉴스레터 전체 모음

---

## 📝 TODO

- [ ] 카테고리별 뉴스레터 생성
- [ ] 사용자 맞춤형 뉴스레터
- [ ] 웹 대시보드 개발
- [ ] A/B 테스트 프레임워크
- [ ] 다국어 지원 (영어, 일본어)

---

**생성일**: 2026-01-30  
**작성자**: AI Workspace Team  
**버전**: 1.0
