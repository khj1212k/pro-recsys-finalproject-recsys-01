# 파이프라인 전역 설정
# - 환경별(dev/staging/prod) 설정 분리
# - LLM, 임베딩, 클러스터링, 크롤러 관련 파라미터 정의
# - RSS 피드 소스 목록 관리

import os
from pathlib import Path
from typing import Dict, Tuple
from dotenv import load_dotenv

load_dotenv(override=False)

# ai_workspace/config/settings.py -> parents[2]가 저장소 루트.
# LLM_KILL_SWITCH_FILE 기본값을 CWD가 아닌 저장소 루트 기준으로 고정하기 위함
# (배치가 어느 디렉터리에서 실행되든 항상 같은 파일을 본다).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def shared_ops_root(repo_root: Path) -> Path:
    """git 워크트리에서 실행 중이면 메인 체크아웃의 루트를, 아니면 repo_root를 돌려준다.

    LLM 지출 원장(docs/adr/0035)과 킬 스위치 파일(docs/adr/0005 부록)의 기본 위치를 정하는 데 쓴다.
    워크트리마다 `.ops/`가 따로면 원장도 따로 생겨 전체 상한이 워크트리 수만큼 늘어나고, 메인
    체크아웃에 켜 둔 킬 스위치를 워크트리의 실험이 보지 못한다. 워크트리의 `.git`은 디렉터리가
    아니라 `gitdir: <메인>/.git/worktrees/<이름>` 한 줄짜리 파일이고, 그 디렉터리의 `commondir`가
    공용 `.git`을 가리킨다 - git을 실행하지 않고 그 두 파일만 읽는다.
    """
    git_entry = repo_root / ".git"
    try:
        if not git_entry.is_file():
            return repo_root
        head = git_entry.read_text(encoding="utf-8").strip()
        if not head.startswith("gitdir:"):
            return repo_root
        gitdir = Path(head.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = repo_root / gitdir
        commondir_file = gitdir / "commondir"
        if not commondir_file.is_file():
            return repo_root  # 서브모듈 등 - 공용 저장소가 따로 없다
        common = (gitdir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        return common.parent if common.name == ".git" else repo_root
    except OSError:
        return repo_root


def resolve_kill_switch_file(raw, repo_root: Path) -> str:
    """LLM_KILL_SWITCH_FILE 값을 실제 경로로 바꾼다.

    - 지정하지 않으면(None) `<메인 체크아웃>/.ops/LLM_KILL_SWITCH`. 워크트리에서도 같은 파일이다.
    - 상대 경로는 실행한 디렉터리가 아니라 메인 체크아웃 기준이다. 이 저장소는 잡을 저장소
      루트에서, main.py를 ai_workspace/에서 실행한다 - CWD 기준이면 서로 다른 파일을 본다.
    - 절대 경로는 그대로. 빈 문자열은 그대로 빈 문자열이다(파일 킬 스위치를 보지 않는 기존 동작).
    """
    root = shared_ops_root(repo_root)
    if raw is None:
        return str(root / ".ops" / "LLM_KILL_SWITCH")
    raw = raw.strip()
    if not raw:
        return ""
    path = Path(raw)
    return str(path if path.is_absolute() else root / path)


class Environment:
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


def get_environment() -> str:
    return os.getenv("ENV", Environment.DEV).lower()


class BaseSettings:

    # ========== LLM Provider ==========
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "naver")  # 'naver' or 'openai'

    # ========== Naver HyperCLOVA X ==========
    NCP_CLOVASTUDIO_API_KEY: str = os.getenv("NCP_CLOVASTUDIO_API_KEY", "")
    NCP_APIGW_API_KEY: str = os.getenv("NCP_APIGW_API_KEY", "")
    HYPERCLOVA_MODEL: str = os.getenv("HYPERCLOVA_MODEL", "HCX-003")

    # ========== OpenAI (Fallback) ==========
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # ========== LangGraph ==========
    MAX_RETRY_CLUSTER_EVAL: int = 2
    MAX_RETRY_NEWSLETTER_EVAL: int = 3
    # (구) MAX_JSON_PARSE_RETRIES: workflow/evaluators.py가 직접 재시도 루프를 돌리던
    # 시절의 상한이었다. core/llm/ 도입(ADR 0005) 이후로는 LLMClient.complete()가
    # MAX_LLM_CALL_RETRIES 하나로 모든 재시도(429/5xx/timeout/스키마 검증 실패)를
    # 통일해서 처리하므로 제거했다.

    # ========== HDBSCAN ==========
    HDBSCAN_MIN_CLUSTER_SIZE: int = int(os.getenv("HDBSCAN_MIN_CLUSTER_SIZE", "3"))
    HDBSCAN_MIN_SAMPLES: int = int(os.getenv("HDBSCAN_MIN_SAMPLES", "2"))
    # 생성할 뉴스레터 최소 목표 수량 (0이면 비활성화, --limit 밖의 클러스터로 보충하지 않음)
    MIN_NEWSLETTER_TARGET: int = int(os.getenv("MIN_NEWSLETTER_TARGET", "0"))
    # 클러스터링 대상 기사의 크롤링 시각 lookback 윈도우(시간). 기본값 24는 기존 동작을 보존한다.
    # noise/평가실패로 뉴스레터화되지 못한 기사(news_letter_id IS NULL)를 더 긴 윈도우(예: 48~72)로
    # 다음 실행에서 재검토할 수 있도록 설정 가능하게 만든 값.
    CLUSTER_LOOKBACK_HOURS: int = int(os.getenv("CLUSTER_LOOKBACK_HOURS", "24"))

    # ========== Embedder ==========
    EMBEDDING_BATCH_SIZE: int = 8  # GPU 메모리 고려
    EMBEDDING_DIM: int = 1024
    # BGE-M3 입력 토큰 상한 - 임베딩 의미가 바뀌는 값이라 ADR 0006의 사전 등록 규칙으로 정했다.
    EMBEDDING_MAX_LENGTH: int = int(os.getenv("EMBEDDING_MAX_LENGTH", "8192"))
    # 배치 크기 x (배치 내 최대 토큰 길이)^2 상한 = 1024토큰 8건. eager attention 점수 텐서 크기를 묶는다.
    EMBEDDING_ATTENTION_BUDGET: int = int(os.getenv("EMBEDDING_ATTENTION_BUDGET", str(8 * 1024 ** 2)))

    # ========== Pipeline ==========
    DEFAULT_CLUSTER_LIMIT: int = None
    # Stage5 클러스터 병렬 처리 스레드 수 (pipeline/stages.py에서 1~4로 제한, ADR 0010)
    NEWSLETTER_WORKERS: int = int(os.getenv("NEWSLETTER_WORKERS", "3"))

    # ========== Quality Thresholds ==========
    # (미사용) 실제 PASS 기준은 아래 JUDGE_* 값이다. MIN_CLUSTER_CONFIDENCE를 게이트로
    # 연결할지는 ClusterEvaluator confidence ROC로 정한다(ADR 0009) - 그 전까지 미사용.
    MIN_CLUSTER_CONFIDENCE: float = 0.7

    # ========== Judge v2 (docs/adr/0010) ==========
    # 기준별(1~5) 최저 점수와 허용할 근거 없는 주장 수. 사람 라벨로 보정하기 전의
    # 잠정값이다(ADR 0009의 2-fold 선택 결과로 교체).
    JUDGE_MIN_CRITERION_SCORE: int = int(os.getenv("JUDGE_MIN_CRITERION_SCORE", "3"))
    JUDGE_MAX_UNSUPPORTED_CLAIMS: int = int(os.getenv("JUDGE_MAX_UNSUPPORTED_CLAIMS", "0"))
    # "enforce": FAIL이면 재생성 / "shadow": 채점된 FAIL은 기록만 하고 통과(점수 없는 FAIL은 막음).
    # 기본 shadow: ADR 0009는 사람 라벨 대비 OOF kappa >= 0.40이 확인된 judge만 게이트로 쓰게
    # 정했고(아직 미측정), 현재 기본 judge는 생성기와 같은 Gemini 계열이다 (ADR 0010).
    JUDGE_GATE_MODE: str = os.getenv("JUDGE_GATE_MODE", "shadow").lower()

    # ========== 결정론적 게이트 (docs/adr/0010) ==========
    # 모드: "enforce"(막고 재생성) / "shadow"(기록만) / "off"
    FAITHFULNESS_GATE_MODE: str = os.getenv("FAITHFULNESS_GATE_MODE", "enforce").lower()
    # 개체명은 한국어 형태소 분석 기반 퍼지 매칭이라 정밀도가 아직 측정되지 않았다 -
    # 사람 사실 오류 라벨로 정밀도를 재기 전까지는 참고용(피드백에만 포함)으로 둔다.
    FAITHFULNESS_BLOCKING_TYPES: str = os.getenv("FAITHFULNESS_BLOCKING_TYPES", "numbers,quotes")
    TONE_DRIFT_GATE_MODE: str = os.getenv("TONE_DRIFT_GATE_MODE", "enforce").lower()
    TONE_DRIFT_BLOCKING_TYPES: str = os.getenv("TONE_DRIFT_BLOCKING_TYPES", "numbers,dates,entities_added")
    # 드리프트가 난 문체 변환을 다시 시도하는 횟수. 소진하면 형식체 초안을 저장한다.
    MAX_RETRY_TONE_DRIFT: int = int(os.getenv("MAX_RETRY_TONE_DRIFT", "1"))
    
    # ========== Pipeline Stages ==========
    STAGE_NAMES: Dict[int, str] = {
        0: "User Embedding",
        1: "RSS Collection",
        2: "Content Extraction",
        3: "Article Embedding",
        4: "Clustering",
        5: "Newsletter Generation",
        6: "Newsletter Embedding",
    }
    
    # ========== Retry Configuration ==========
    RETRY_EXPONENTIAL_BASE: float = 2.0
    MAX_RETRY_WAIT_SECONDS: int = 64
    # RSS/본문 요청에 쓰는 User-Agent. 라이브러리 기본 UA를 막는 언론사가 있다(한국경제 RSS: feedparser UA에 403).
    HTTP_USER_AGENT: str = os.getenv(
        "HTTP_USER_AGENT", "Mozilla/5.0 (compatible; newsletter-recsys/1.0)"
    )
    MAX_FETCH_RETRIES: int = 3  # RSS/본문 크롤링 네트워크 요청 최대 재시도 횟수
    # 본문 다운로드가 (위 재시도까지) 실패한 기사를 이후 실행에서 다시 시도하는 총 횟수 상한.
    # news_raw.raw_news_extract_attempts로 센다 - 막힌 URL을 2시간마다 영원히 두드리지 않게.
    MAX_EXTRACT_ATTEMPTS: int = int(os.getenv("MAX_EXTRACT_ATTEMPTS", "3"))
    # LLM 채팅 API(HyperCLOVA/OpenAI) 호출 재시도 최대 횟수 (감사에서 발견: HyperCLOVA
    # 클라이언트의 `while True` 루프가 이 상한 없이 무제한 재시도했음)
    MAX_LLM_CALL_RETRIES: int = 10
    # SDK 기본 타임아웃(600초)과 SDK 자체 재시도(2회)가 우리 재시도와 겹쳐 한 호출이 수 분씩
    # 멈추는 것을 2026-09-25 실제 Gemini 503/timeout 상황에서 확인했다.
    LLM_REQUEST_TIMEOUT_S: float = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "60"))
    LLM_CALL_DEADLINE_S: float = float(os.getenv("LLM_CALL_DEADLINE_S", "180"))
    # ToneConverter.convert()가 validate_conversion() 실패 시 추가로 재생성을
    # 시도하는 횟수(최초 1회 + 이 값만큼 추가). 전송 계층 재시도(429/5xx/timeout)는
    # core/llm/adapters.py::OpenAICompatLLMClient.complete() 내부에서 이미 처리되므로
    # 이 값은 "콘텐츠가 검증을 통과하지 못했을 때"만 적용된다.
    MAX_RETRY_TONE_VALIDATION: int = 2

    # ========== LLM Kill Switch ==========
    # 예정된 비용 가드(cron)가 실제 Google Cloud 과금이 시작되면 이 파일을 만들어
    # 킬 스위치를 켠다. env LLM_KILL_SWITCH("1"/"true"/"yes")는
    # core/llm/kill_switch.py가 호출마다 직접 os.getenv로 읽는다(여기 캐싱하면
    # 테스트/런타임에서 즉시 반영되지 않음). 파일 경로만 여기서 정의한다 - CWD가 pipeline
    # 실행 위치에 따라 달라져도, git 워크트리에서 실행해도 항상 같은 파일(메인 체크아웃의
    # .ops/LLM_KILL_SWITCH)을 가리켜야 하기 때문(resolve_kill_switch_file, 지출 원장과 같은 위치).
    LLM_KILL_SWITCH_FILE: str = resolve_kill_switch_file(os.getenv("LLM_KILL_SWITCH_FILE"), _REPO_ROOT)

    # ========== LLM 지출 상한 (docs/adr/0035) ==========
    # 아래는 같은 이름(_DEFAULT 뺀)의 환경변수가 없을 때 쓰는 기본값이다. 환경변수는
    # core/llm/budget.py가 호출마다 읽는다(킬 스위치와 같은 이유 - 실행 중에 바꾼 값이 다음
    # 호출부터 적용돼야 한다). 값이 숫자로 해석되지 않으면 기본값으로 넘어가지 않고 호출을 거부한다.
    # 금액은 세전 USD다. 기본 전체 상한 $3.00은 가정 환율 ₩1,400/$로 ₩4,200이다.
    LLM_BUDGET_RUN_USD_DEFAULT: str = "0.20"    # 런(프로세스 1회 실행, 또는 LLM_RUN_ID가 같은 실행들)
    LLM_BUDGET_DAY_USD_DEFAULT: str = "0.30"    # LLM_BUDGET_DAY_TZ 기준 하루
    LLM_BUDGET_TOTAL_USD_DEFAULT: str = "3.00"  # 원장 파일 전체
    LLM_BUDGET_DAY_TZ_DEFAULT: str = "Asia/Seoul"
    # 원장 기본 위치. 워크트리에서 돌려도 메인 체크아웃의 .ops/를 가리킨다(shared_ops_root).
    LLM_SPEND_LEDGER_FILE_DEFAULT: str = str(
        shared_ops_root(_REPO_ROOT) / ".ops" / "llm_spend_ledger.jsonl"
    )
    # 정산 없이 이 시간을 넘긴 예약은 예약액 그대로 지출로 확정한다. 시도 1회는 요청 타임아웃
    # (LLM_REQUEST_TIMEOUT_S, 기본 60초)을 넘지 못하므로 그보다 충분히 길게 둔다.
    LLM_BUDGET_RESERVATION_TTL_S_DEFAULT: str = "900"
    # 프롬프트 토큰 상한 추정: UTF-8 바이트 수 / 이 값. 1.0이면 토크나이저와 무관한 상한이다
    # (서브워드 토큰은 최소 1바이트). 실측 비율을 얻은 뒤에만 올린다.
    LLM_BUDGET_BYTES_PER_TOKEN_DEFAULT: str = "1.0"
    # 요약 CLI가 원화를 함께 보여줄 때 쓰는 가정 환율(실제 청구 환율이 아니다).
    LLM_BUDGET_KRW_PER_USD_DEFAULT: str = "1400"
    # 연속 인프라 실패(5xx·타임아웃·연결 오류)가 이 횟수에 닿으면 런을 멈춘다. 0이면 세지 않는다
    # (HTTP 402는 값과 무관하게 한 번에 멈춘다). 기본 5는 Stage5 워커 상한(4)보다 커서, 워커들이
    # 같은 순간에 한 번씩 실패한 것만으로는 닿지 않는다.
    LLM_CIRCUIT_BREAKER_THRESHOLD_DEFAULT: str = "5"
    
    # ========== Crawler Settings ==========
    PARALLEL_WORKERS: int = 8  # 병렬 크롤링 워커
    REQUEST_TIMEOUT: int = 15
    SELENIUM_PAGE_LOAD_TIMEOUT: int = 60

    # ========== SSL Verification ==========
    SSL_VERIFY: bool = True

    # ========== Logging ==========
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = False

    # ========== Database Connection ==========
    # db/connection.py가 os.getenv를 직접 호출하지 않고 이 값을 참조하도록 중앙화함
    # (이전에는 db/connection.py가 자체 os.getenv 기본값("password")을 갖고 있어
    # .env.example의 기본값("recsyspeople")과 서로 달랐음)
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: str = os.getenv("DB_PORT", "5432")
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "final_db")

    # ========== Database Pool ==========
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ========== RSS Feeds ==========
    RSS_FEEDS: Dict[str, Tuple[str, str]] = {
        # 종합 일간지
        '동아일보': ('direct', 'https://rss.donga.com/total.xml'),
        '경향신문': ('direct', 'https://www.khan.co.kr/rss/rssdata/total_news.xml'),
        '매일경제': ('direct', 'https://www.mk.co.kr/rss/30000001/'),
        '한국경제': ('direct', 'https://www.hankyung.com/feed/all-news'),
        '국민일보': ('direct', 'https://www.kmib.co.kr/rss/data/kmibRssAll.xml'),
        '세계일보': ('direct', 'https://www.segye.com/Articles/RSSList/segye_recent.xml'),

        # 과학/기술
        '전자신문_IT': ('direct', 'http://rss.etnews.com/03.xml'),
        '전자신문_AI': ('direct', 'http://rss.etnews.com/04046.xml'),
        '전자신문_과학': ('direct', 'http://rss.etnews.com/20.xml'),
        'AI타임스': ('direct', 'https://www.aitimes.com/rss/allArticle.xml'),
    }

    # ========== User Agent ==========
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


class DevSettings(BaseSettings):
    LOG_LEVEL: str = "DEBUG"
    LOG_TO_FILE: bool = False
    SSL_VERIFY: bool = False  
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5


class StagingSettings(BaseSettings):
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10


class ProdSettings(BaseSettings):
    LOG_LEVEL: str = "WARNING"
    LOG_TO_FILE: bool = True
    SSL_VERIFY: bool = True
    DB_POOL_MIN: int = 5
    DB_POOL_MAX: int = 20
    PARALLEL_WORKERS: int = 8


def get_settings() -> BaseSettings:
    env = get_environment()
    settings_map = {
        Environment.DEV: DevSettings,
        Environment.STAGING: StagingSettings,
        Environment.PROD: ProdSettings,
    }
    return settings_map.get(env, DevSettings)()


Settings = get_settings()
