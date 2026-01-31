# AI Workspace - LangGraph News Pipeline

뉴스 클러스터링 및 뉴스레터 생성을 위한 LangGraph 기반 파이프라인입니다.

## 주요 기능

- **HDBSCAN 클러스터링**: 유사한 뉴스 기사를 자동으로 그룹화
- **LLM 기반 클러스터 평가**: 클러스터가 단일 이벤트/주제를 대표하는지 검증
- **뉴스레터 자동 생성**: LLM을 활용한 통합 뉴스 브리핑 생성
- **품질 평가 및 피드백 루프**: 생성된 뉴스레터의 품질을 평가하고 필요시 재생성
- **문체 변환**: 공식적인 뉴스를 친근한 톤으로 변환 (이모티콘 포함)
- **사용자 임베딩**: Time-decay weighted average 알고리즘으로 개인화된 사용자 프로파일 생성
- **통합 LLM 클라이언트**: Naver HyperCLOVA X 및 OpenAI 지원

## 설치

```bash
cd pro-recsys/ai_workspace
pip install -r requirements.txt
```

## 설정

1. `.env.example`을 `.env`로 복사
2. LLM API 키 설정 (Naver HyperCLOVA X 또는 OpenAI)
3. 데이터베이스 연결 정보 확인

```bash
cp .env.example .env
# .env 파일 편집:
# - LLM_PROVIDER=naver (또는 openai)
# - NCP_CLOVASTUDIO_API_KEY=your_key (Naver 사용시)
# - OPENAI_API_KEY=your_key (OpenAI 사용시)
# - DB 연결 정보
```

## 사용법

### 뉴스레터 파이프라인

```bash
# 전체 파이프라인 실행
python main.py

# 상위 5개 클러스터만 처리
python main.py --limit 5

# 데이터베이스 상태 확인
python main.py --status

# 클러스터링 파라미터 조정
python main.py --min-cluster 5 --min-samples 3
```

### 사용자 임베딩 업데이트

```bash
# 임베딩이 없는 신규 사용자만 업데이트
python update_user_embeddings.py --all

# 모든 사용자 강제 업데이트
python update_user_embeddings.py --all --force-all

# 특정 사용자만 업데이트
python update_user_embeddings.py --user-ids 1,2,3,4,5
```

## 프로젝트 구조

```
ai_workspace/
├── main.py              # 메인 진입점
├── requirements.txt     # 의존성
├── .env.example         # 환경변수 템플릿
├── config/
│   └── settings.py      # 설정값
├── db/
│   └── connection.py    # DB 연결
├── core/
│   ├── clusterer.py     # HDBSCAN 클러스터링
│   └── reconstructor.py # GPT 뉴스레터 생성
└── workflow/
    ├── state.py         # LangGraph 상태 정의
    ├── nodes.py         # 노드 함수들
    ├── evaluators.py    # LLM 평가자
    └── graph.py         # 워크플로우 그래프
```

## LangGraph 워크플로우

```
┌─────────────────┐
│  init_cluster   │◄──────────────────┐
└────────┬────────┘                   │
         │                            │
         ▼                            │
┌─────────────────┐                   │
│  eval_cluster   │                   │
└────────┬────────┘                   │
         │                            │
    ┌────┴────┐                       │
    │         │                       │
  PASS      FAIL                      │
    │         │                       │
    ▼         ▼                       │
┌─────────┐ ┌──────────────┐          │
│gen_news │ │handle_c_fail │──────────┤
└────┬────┘ └──────────────┘          │
     │                                │
     ▼                                │
┌─────────────────┐                   │
│  eval_news      │                   │
└────────┬────────┘                   │
         │                            │
    ┌────┼────┐                       │
    │    │    │                       │
  PASS RETRY MAX                      │
    │    │    │                       │
    ▼    │    ▼                       │
┌─────────┐│ ┌────────────┐           │
│embed_nl ││ │max_retries │───────────┤
└────┬────┘│ └────────────┘           │
     │     │                          │
     ▼     │                          │
┌─────────┐│                          │
│conv_tone││                          │
└────┬────┘│                          │
     │     │                          │
     ▼     │                          │
┌─────────┐│                          │
│  save   ││                          │
└────┬────┘│                          │
     │     │                          │
     └─────┴──────────────────────────┘
```

Note: 
- embed_nl: 원본(공식) 텍스트로 임베딩 생성
- conv_tone: 친근한 톤으로 변환 (이모티콘 포함)
- save: 변환된 텍스트 + 원본 임베딩 저장

## 데이터베이스 요구사항

### user_newsletter_ctr_log 테이블

사용자 임베딩 시스템이 작동하려면 백엔드에서 다음 테이블을 생성해야 합니다:

```sql
CREATE TABLE user_newsletter_ctr_log (
    log_id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES "user"(user_id) ON DELETE CASCADE,
    news_letter_id INTEGER NOT NULL REFERENCES news_letter(news_letter_id) ON DELETE CASCADE,
    read_duration_seconds INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Important**: `interaction_type` 컬럼은 제거되었습니다. 모든 로그는 읽기 이벤트로 간주됩니다.

상세 스키마는 `db/USER_CTR_LOG_SCHEMA.md` 참조

### 사용 가능한 API

`db/user_log_queries.py`에서 제공하는 헬퍼 함수들:
- `log_newsletter_read()`: 뉴스레터 읽기 로그 기록
- `get_user_read_history()`: 사용자 읽기 이력 조회
- `get_newsletter_read_count()`: 뉴스레터 읽기 횟수
- `get_user_interaction_stats()`: 사용자 통계
- `get_popular_newsletters()`: 인기 뉴스레터 순위
- `has_user_read_newsletter()`: 읽기 여부 확인
