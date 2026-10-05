"""train: 은퇴한 잡 - 팀 시절의 학습·추론 레시피(recommend_engine/main_lgbm.py)를 더 돌리지 않는다 (ADR 0033).

이 잡은 팀 레시피(binary 목적함수 + 클릭당 무작위 네거티브 5개, 성승우 설계·구현)로 모델을 학습하고 그 추론
결과를 news_letter_today_batch에 써 왔다. 돌리지 않는 이유:
- 그 레시피의 평가 수치는 ADR 0007에서 철회했고, 랭킹은 ADR 0013의 ranker v2가 대체했다.
- 서빙이 읽는 모델은 model_registry의 행이다(scripts/register_model.py). 요청 시점에 채점하는 피처는
  recsys_core 서빙 어댑터가 만들고, 이 레시피의 피처 코드(recommend_engine의 FeatureEngineer)와 정의가 다르다.
  이 잡이 만든 모델은 서빙 피처로 채점할 수 없다.
- realtime 모드는 배치 행을 읽지 않는다(ADR 0015 개정). 이 잡이 쓰던 표는 RECSYS_MODE=batch에서만 읽힌다.

코드는 지우지 않았다: 레시피 자체(ai_workspace/recommend_engine)는 EB-NeRD 하네스의 ablation 시작점
team_binary(evaluation/recsys/ebnerd/models.py)의 원본으로 남아 있다. 이 잡은 스케줄이나 손으로 불려도
아무것도 실행하지 않고 건너뛴 것으로 기록된다(job_runs: skipped, team_recipe_retired).
"""
from typing import Any, Dict

from jobs.runtime import JobSkipped

RETIRED_REASON = "team_recipe_retired"


def add_arguments(parser) -> None:
    # 예전 명령줄(--no-inference)이 사용법 오류가 되지 않게 받기만 한다.
    parser.add_argument("--no-inference", action="store_true", help="(무시됨: 이 잡은 은퇴했다)")


def run(ctx) -> Dict[str, Any]:
    raise JobSkipped(RETIRED_REASON, {"replacement": "scripts/register_model.py (ADR 0033)"})
