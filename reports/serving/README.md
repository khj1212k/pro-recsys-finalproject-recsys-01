# reports/serving - 요청 시점 추천 지연 측정 결과

`evaluation/serving/request_path_bench.py`를 GitHub Actions `recsys-bench` 워크플로
(`.github/workflows/recsys-bench.yml`)에서 돌려 받은 결과 JSON 원본이다. 해석은
[ADR 0015](../../docs/adr/0015-request-time-recommendation.md)와
[ADR 0017](../../docs/adr/0017-short-term-state-store.md)에 있다.

| 파일 | CI 실행 | 커밋 | 실행 시각(UTC) | 무엇을 잰 코드인가 |
|---|---|---|---|---|
| `request_path_bench_ci_36168597131.json` | 36168597131 | `5e84358` | 2026-09-25 17:40 | 브랜치를 main 위로 옮기기 전의 끝 커밋(지금 이력에는 없다). 같은 코드의 현재 커밋은 `66657a6` |
| `request_path_bench_ci_37348628265.json` | 37348628265 | `66657a6` | 2026-10-05 17:28 | 위와 같은 요청 경로 코드, main 위로 옮긴 직후. 러너 간 편차를 보려고 함께 둔다 |
| `request_path_bench_ci_37350694155.json` | 37350694155 | `b66dd99` | 2026-10-05 17:44 | 리뷰 수정(표시 가능 필터, 보조 풀, pre-ping 등)을 넣은 코드 |

표의 커밋 SHA는 실행 당시의 것이다. 브랜치를 앞선 브랜치 위에 다시 쌓으면서(2026-10-06) `66657a6`은 `264be0d`,
`b66dd99`는 `d925af0`이 됐고, 측정 대상 코드의 git 객체 해시는 같다(ADR 0015 "통합 기록"). 세 번째 JSON 안의
`commit` 값은 고치지 않았다.

## 조건 (세 실행 공통)

- 러너: GitHub 호스티드 `ubuntu-latest`(x86_64, Ubuntu 24.04, 4 vCPU - 코어 수는 세 번째 실행부터 결과에
  기록했다). Postgres(`pgvector/pgvector:pg16`)와
  Redis 7 서비스 컨테이너, 부하를 만드는 파이썬 스레드가 **같은 러너에서** 돈다.
- 데이터는 전부 합성이다: 뉴스레터 3만 개(72시간 신선도 창 안 516개), 사용자 2만 명(장기 벡터 없는
  사용자 1,000명), 클릭 1,015,000건(최근 24시간 활동 사용자 8,664명, 그중 24시간에 30번 클릭한 사용자 500명).
  임베딩은 주제 8개의 중심 + 잡음(1024차원)이라 **지연과 쿼리 계획만** 본다. 추천 품질 수치가 아니다.
- 쿼리 단위 측정은 한 커넥션에서 순차 400회(아이템 300개 조회만 40회).
- 서비스 수준 측정은 운영 시간 예산 300ms, 결과 캐시 끔(`cache_ttl_s=0`), 앱 풀 5+10(대기 한도 10초),
  동시 클라이언트 1/4/8/16/32, 배선 순서를 바꿔 가며 3라운드. 클라이언트당 요청 수는 1 클라이언트 400회,
  그 밖에는 합계 400회(32 클라이언트는 800회).

## 읽을 때 주의할 점

- 앞의 두 파일은 서비스 수준 절의 키가 `end_to_end`이고 세 번째 파일부터 `service_level`이다. 이름만 바꿨고
  측정 방법은 같다. 이 절은 **HTTP 측정이 아니다**: `service.recommend`를 직접 부르므로 HTTP 파싱, JWT 검증,
  표시 단계(hydrate), JSON 직렬화, 노출 로그 쓰기가 들어 있지 않다.
- 같은 코드(`5e84358`과 `66657a6`)인데도 두 실행의 1 클라이언트 p50이 14ms와 11~12ms로 약 20% 다르다.
  호스티드 러너는 실행마다 다른 VM이다. 세 번째 실행과 앞 두 실행의 차이를 코드 변경의 효과로 읽을 수 없다.
- 배포 대상(Oracle A1, arm64 2 OCPU)에서의 값이 아니다. 그 환경에서의 측정은 아직 하지 않았다
  (ADR 0015 "결과와 한계").

## 다시 만들기

워크플로를 수동 실행(`workflow_dispatch`)하거나, Postgres(+pgvector)와 Redis가 있는 곳에서 직접 돌린다.
일회용 데이터베이스 `recsys_bench_<hex>`를 만들고 끝나면 지운다. 기존 데이터베이스에는 쓰지 않는다.

```bash
python -m evaluation.serving.request_path_bench \
  --server-url postgresql://<user>:<password>@<host>:5432/postgres \
  --redis-url redis://<host>:6379/0 \
  --out recsys-bench.json
```
