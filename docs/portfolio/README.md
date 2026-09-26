# 포트폴리오 사례 연구

이 디렉터리는 이 저장소에서 나온 결과를 기술 리뷰어가 검증할 수 있는 형태로 정리한 사례 연구 모음이다. 각 문서는 다음 규칙을 따른다.

- **모든 수치는 저장소 안의 리포트 파일(`reports/**/*.json`)이나 코드 줄 번호로 추적 가능**해야 한다. 그림은 `scripts/`의 스크립트가 그 JSON에서 다시 그린다.
- **증거 유형 라벨**을 붙인다: **[synthetic team data]** 팀이 아카이브한 LLM 생성 합성 클릭 · **[EB-NeRD]** 덴마크어 공개 뉴스 클릭 로그(연구용, 집계 수치만 커밋) · **[KR-eval]** 실제 한국어 기사 위의 LLM 생성 평가 · **[SIM]** 유저 시뮬레이터 · **[LOAD]** 부하 시험. 라벨과 함께 n과 한계를 적는다.
- 팀 프로젝트(부스트캠프 AI Tech 8 RecSys, 5인, 2026-02-10 종료)의 결과물은 팀과 담당 팀원의 것으로 표기하고, 종료 이후 fork에서 단독으로 한 일과 구분한다.
- 어드버서리얼 방법론 검토를 통과한 주장만 사실로 적는다. 검토 전이거나 증거가 아직 없는 것은 **진행 중**으로 표시한다.

## 목록

| # | 사례 연구 | 상태 | 증거 유형 | 1차 출처 |
|---|---|---|---|---|
| 01 | [팀이 보고한 MRR 0.897은 왜 부풀려졌나 — 추천 평가 누수 재현과 프로토콜 재설계](01-evaluation-leakage.md) | 초안 완료 (v2 리포트 "sound-with-caveats"; 남은 수정 사항은 문서 8절) | [synthetic team data] | `reports/recsys/team_repro_v2.{md,json}`, ADR 0007 (브랜치 `eval/team-baseline-repro-v1`) |
| 02 | EB-NeRD에서 팀 방식 LightGBM → ranker v2까지 단계별 ablation | **진행 중** | [EB-NeRD] | `reports/recsys/ebnerd_v1.{md,json}`, ADR 0013 (브랜치 `eval/ebnerd-harness`) |
| 03 | LLM 뉴스레터 생성의 사실성 게이트와 judge 보정 | **진행 중** | [KR-eval] | ADR 0009(사전 등록 bake-off), ADR 0010 (브랜치 `eval/llm-bakeoff-and-gate`) |
| 04 | 요청 시점(request-time) 추천 서빙과 학습/서빙 parity | **진행 중** | [LOAD], [SIM] | 브랜치 `feat/realtime-recommendation`, `feat/user-simulator-loadtest` |

## 진행 중인 사례 연구가 기다리는 증거

**02 EB-NeRD ranker ablation.** 리포트 v1이 브랜치에 있으나 아직 어드버서리얼 방법론 검토를 거치지 않았고 main에 병합되지 않았다. 사례 연구로 쓰기 전에 필요한 것: (a) 사전 등록 커밋(ADR 0013) 대비 결과 후 변경 사항이 "사후 변경 기록"에만 있는지 대조, (b) 7단계 ablation 각 행의 유저 클러스터 부트스트랩 CI와 전 시드 `best_iteration > 5` 확인, (c) 노출 재정렬(P1)과 48h 전체 풀 랭킹(P2)에서 결론이 갈리는 지점을 "네거티브 분포가 과제를 정의한다"는 서술로 정리, (d) 검토 통과. EB-NeRD 원본·임베딩은 라이선스상 이 Mac 밖으로 나가지 않으며 기사 텍스트는 인용하지 않는다.

**03 LLM 사실성 게이트.** ADR 0009가 결과 없는 시점에 사전 등록한 bake-off 판정 규칙과, ADR 0010의 결정론적 사실 추출기(`evaluation/llm/faithfulness.py`) 기반 게이트가 있다. 필요한 것: (a) 실제 한국어 기사 원문과 생성물이 짝지어진 데이터 위의 차단율, (b) 사람 라벨로 보정한 judge의 q0/q1과 그로 보정한 미지원율(라벨 n 명시), (c) 게이트가 잡은 오류 유형 분포. 그 전까지는 게이트 설계와 사전 등록 절차만 서술 가능하다.

**04 요청 시점 서빙.** `GET /newsletters/today`의 요청 시점 파이프라인(후보 합집합 → 스코어링 → MMR)이 브랜치에 있다. 필요한 것: (a) 학습 피처 코드와 서빙 피처 코드가 같은 함수를 타는지 확인하는 parity 테스트(피처 max|Δ|, 순위 일치), (b) 시드 DB 위 재생 부하 시험의 지연 분포 [LOAD], (c) 시뮬레이터로 정책 비교 추정치(SNIPS/replay)가 실측과 일치하는지 [SIM]. "<100ms" 같은 수치는 부하 시험 리포트가 커밋되기 전에는 쓰지 않는다.

## 디렉터리 구조

```
docs/portfolio/
├── README.md                    # 이 문서
├── 01-evaluation-leakage.md     # 사례 연구 01
├── img/                         # 그림 (light/dark 두 변형, scripts/가 생성)
├── data/                        # 본문 수치 발췌본 (출처 SHA·데이터 해시 포함)
└── scripts/                     # 그림·발췌본 생성 스크립트 (reports/*.json → img/, data/)
```
