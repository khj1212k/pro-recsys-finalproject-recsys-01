"""train: LightGBM(LambdaRank) 학습 + 추론(recommend_engine/main_lgbm.py --train --inference).

recommend_engine은 `from src...` 임포트와 상대 경로(config/, checkpoints/)를 쓰는 독립
서브 프로젝트라 같은 프로세스에 섞지 않고 자식 프로세스로 실행한다.
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict

from jobs import REPO_ROOT

ENGINE_DIR = REPO_ROOT / "ai_workspace" / "recommend_engine"


def add_arguments(parser) -> None:
    parser.add_argument("--no-inference", action="store_true", help="학습만 하고 추론 결과 저장은 건너뜀")


def run(ctx) -> Dict[str, Any]:
    cmd = [sys.executable, "main_lgbm.py", "--train"]
    if not ctx.args.no_inference:
        cmd.append("--inference")
    ctx.stats["command"] = " ".join(cmd[1:])

    proc = subprocess.run(cmd, cwd=ENGINE_DIR, env=os.environ.copy())
    ctx.stats["returncode"] = proc.returncode
    if proc.returncode != 0:
        raise RuntimeError(f"main_lgbm.py가 종료 코드 {proc.returncode}로 실패했습니다")

    pointer_path = ENGINE_DIR / "checkpoints" / "latest_model.json"
    if pointer_path.exists():
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        return {"model": {k: pointer.get(k) for k in ("version", "model_file", "metrics")}}
    return {}
