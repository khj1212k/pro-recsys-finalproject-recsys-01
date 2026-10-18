## 요약
- 무엇을 왜 바꿨는지 2~3줄. 관련 이슈나 ADR이 있으면 번호를 적는다.

## 변경 내용
- `경로`: 주요 변경 사항 (2줄 이내, 길어지면 항목을 나눈다)

## 검증
- 실행한 명령과 결과 (예: `make test TESTS=tests/recsys` 통과 건수). 돌리지 않은 것은 "돌리지 않음"과 이유를 적는다.
- 통합 테스트(Postgres)·이미지 빌드처럼 CI에서만 도는 것은 CI 결과에 맡긴다고 적는다.

## 주장 범위 (수치나 결과를 쓴 PR만)
- [ ] 수치는 커밋된 `reports/`·`docs/adr/`의 값만 썼고 출처 경로를 달았다
- [ ] 데이터 라벨을 붙였다 (`[EB-NeRD]` `[SIM]` `[KR-*]` `[team-synth]` 등)
- [ ] 측정하지 않은 것은 "측정하지 않음"으로 썼다

## 안전 점검
- [ ] 키·토큰·서명 URL·`.env` 값이 없다 (CI의 `secret-scan`이 새 커밋을 본다)
- [ ] 한국어 기사 본문·생성 초안·EB-NeRD 기사 텍스트를 넣지 않았다 ([ADR 0023](https://github.com/khj1212k/pro-recsys-finalproject-recsys-01/blob/main/docs/adr/0023-data-sources-copyright-retention.md)). 예시는 지어낸 텍스트이고 그렇게 표시했다
- [ ] 의존성을 바꿨다면 `requirements-ci.in`을 고치고 `make ci-lock`까지 했다 (`tests/test_ci_requirements_lock.py`가 어긋남을 잡는다)
- [ ] 설계·기술 선택이 바뀌었으면 관련 `docs/adr/`에 절이나 개정을 더하고 `docs/adr/README.md` 색인을 맞췄다

## 참고 (선택)
- 리뷰어가 알면 좋은 것, 이 PR에 넣지 않은 후속 작업
