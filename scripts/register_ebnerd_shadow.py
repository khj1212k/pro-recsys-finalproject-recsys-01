#!/usr/bin/env python3
"""EB-NeRD M4 판정용 실행에서 학습한 LightGBM 모델을 shadow로 등록한다 (ADR 0037, ADR 0013 "A2 결과" 후속 작업 1).

모델 파일은 git에 없고, 이 스크립트도 모델을 저장소에 두지 않는다. 등록하는 사람이 배포할 때 파일을 넘긴다:
M4 체크포인트(`<체크포인트>/models/<arm>_s<seed>.txt`와 실행기가 옆에 쓴 같은 이름의 `.json`)를 서빙 DB에 닿는
곳의 임시 디렉터리로 옮기고(scp, 또는 그 실행의 비공개 버킷에 아직 있으면 거기서 내려받기), 등록한 뒤 지운다.

    python scripts/register_ebnerd_shadow.py --model /tmp/m4/poolneg_s0.txt --model /tmp/m4/poolneg_mask0_s0.txt --dry-run
    DATABASE_URL=postgresql://... python scripts/register_ebnerd_shadow.py \\
        --model /tmp/m4/poolneg_s0.txt --model /tmp/m4/poolneg_mask0_s0.txt

`--model`을 여러 번 주면 그 순서대로 하나씩 등록한다. 서빙은 shadow를 최근 등록 순으로 RECSYS_SHADOW_MAX개 읽으므로
주모델(`poolneg_mask0`)을 마지막에 둔다(첫 shadow 칸).

**하나라도 어긋나면 등록하지 않는다** (scripts/register_model.py의 검사에 더해):
1. 모델 파일 옆에 실행기 요약(`.json`)이 있고, 그 요약의 arm·seed가 파일 이름(`<arm>_s<seed>.txt`)과 같고,
   등록을 허용한 arm·seed(ADR 0037 결정 1·2: `poolneg_mask0`·`poolneg`, seed 0)다.
2. 그 요약이 커밋된 판정 리포트(`reports/recsys/ebnerd_v1_2_cold.json`의 `models.<arm>`의 같은 seed)와 피처 목록·
   best_iteration이 같다 - 판정에 쓴 바로 그 학습의 산출물인지 보는 것이다(리포트에 모델 파일의 해시는 없다).
3. 모델 본문의 트리 수가 best_iteration과 같다 - 하네스는 best_iteration까지로 예측했고, 서빙은 본문의 트리를 전부 쓴다.
4. 모델의 열 이름·순서가 서빙 어댑터(`recsys_core.serving.FEATURE_NAMES`, 22열)와 같다. 다르면 여기서 멈춘다 -
   열을 맞춰 끼워 넣지 않는다.
5. 모델 파일이 이 저장소의 작업 트리 안에 있으면 git이 추적하지 않고 무시 목록에 걸린 자리여야 한다.

등록하는 행: role = shadow만(이 스크립트는 active를 받지 않는다 - ADR 0013 결정 4의 활성 조건이 남아 있다),
버전 `ebnerd-v1.2-<arm>-s<seed>`, 스키마 지문 = 서빙 어댑터의 SCHEMA_HASH. `metrics`에 출처(리포트 파일 sha256,
사전 등록 커밋, 코드 SHA, 증거 등급), 학습한 쪽의 피처 정의와 서빙 정의의 차이, 라이선스 범위를 함께 적는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import register_model  # noqa: E402  (scripts/register_model.py)
from register_model import ModelRejected  # noqa: E402
from recsys_core import serving  # noqa: E402

REPORT = REPO / "reports" / "recsys" / "ebnerd_v1_2_cold.json"
ADR = "docs/adr/0037-ebnerd-shadow-model-registration.md"
# ADR 0037 결정 1·2: 등록을 허용한 arm과 seed. 다른 arm(랭크 정규화 E3 불충족, 축소 CTR E6 미채택, NaN 마스킹 등)은
# 받지 않는다.
REGISTRABLE = {
    "poolneg_mask0": "shadow 주모델 (ADR 0013 A2 결과 E1(a))",
    "poolneg": "둘째 shadow 칸 - E1(a)에서 주모델과 비교한 원 모델 (ADR 0037 결정 2)",
}
SEEDS = (0,)
LICENSE = (
    "EB-NeRD는 연구·비상업 전용이다. 이 모델은 비상업 포트폴리오 배포의 shadow 점수(칸 로그)에만 쓴다. "
    "모델 본문은 22개 피처에 대한 분기 임계값과 잎 값뿐이고 EB-NeRD의 기사 텍스트·임베딩·사용자 ID·행 데이터는 "
    "들어 있지 않으며 함께 배포하지 않는다. 저장소·공개 위치에 두지 않는다 (ADR 0037)."
)
_NAME = re.compile(r"^(?P<arm>[a-z0-9_]+)_s(?P<seed>\d+)$")


def training_definition_differences() -> dict:
    """하네스가 학습할 때의 피처 정의(recsys_core.features.FeatureConfig 기본값, 데이터의 세션 ID)와 서빙 어댑터의
    정의(SERVING_FEATURE_CONFIG)가 다른 곳. 열 이름·순서는 같고 값을 만드는 규칙만 다르다(ADR 0033 결정 5)."""
    from recsys_core.features import FeatureConfig

    harness, srv = FeatureConfig(), serving.SERVING_FEATURE_CONFIG

    def norm(v):
        return [float(x) for x in v] if isinstance(v, (list, tuple)) else v

    out = {}
    for field in ("half_life_days", "floor_days", "min_weight", "short_window_h", "short_max_events",
                  "pop_windows_h", "ctr_window_h", "item_lag_s"):
        a, b = norm(getattr(harness, field)), norm(getattr(srv, field))
        if a != b:
            out[field] = {"training": a, "serving": b}
    out["session"] = {
        "training": "EB-NeRD session_id",
        "serving": f"{serving.SCHEMA_DEFINITION['session_gap_s']}s gap over the latest {serving.SHORT_MAX_EVENTS} clicks",
    }
    return out


def _git(*args: str) -> int:
    return subprocess.run(["git", "-C", str(REPO), *args], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def check_location(path: Path) -> None:
    """5번 검사. 저장소 밖의 파일이거나 git이 없는 곳(컨테이너)이면 통과."""
    try:
        rel = path.resolve().relative_to(REPO)
    except ValueError:
        return
    if not (REPO / ".git").exists():
        return
    if _git("ls-files", "--error-unmatch", str(rel)) == 0:
        raise ModelRejected(f"{rel}은 git이 추적하는 파일입니다. EB-NeRD로 학습한 모델은 저장소에 두지 않습니다(ADR 0037).")
    if _git("check-ignore", "-q", str(rel)) != 0:
        raise ModelRejected(
            f"{rel}은 저장소 안의 무시되지 않는 자리입니다(커밋될 수 있다). 저장소 밖이나 gitignore된 디렉터리에서 넘기세요."
        )


def arm_and_seed(model_path: Path) -> tuple[str, int]:
    m = _NAME.match(model_path.stem)
    if not m or model_path.suffix != ".txt":
        raise ModelRejected(f"파일 이름이 실행기의 모델 이름(<arm>_s<seed>.txt)이 아닙니다: {model_path.name}")
    return m["arm"], int(m["seed"])


def report_entry(report: dict, arm: str, seed: int) -> dict:
    for entry in report.get("models", {}).get(arm, []):
        if int(entry["seed"]) == seed:
            return entry
    raise ModelRejected(f"판정 리포트에 {arm} seed {seed}가 없습니다")


def build_row(model_text: str, sidecar: dict, report: dict, arm: str, seed: int, name: str = "ranker",
              source: Optional[str] = None, report_sha256: Optional[str] = None,
              report_path: str = "reports/recsys/ebnerd_v1_2_cold.json") -> dict:
    """등록할 행. 검사(모듈 설명의 1~4)를 통과하지 못하면 ModelRejected."""
    if arm not in REGISTRABLE or seed not in SEEDS:
        raise ModelRejected(
            f"{arm} seed {seed}는 등록을 허용한 모델이 아닙니다(허용: {sorted(REGISTRABLE)} x seed {list(SEEDS)}, ADR 0037)"
        )
    if sidecar.get("model") != arm or int(sidecar.get("seed", -1)) != seed:
        raise ModelRejected(
            f"실행기 요약의 arm·seed({sidecar.get('model')!r}, {sidecar.get('seed')!r})가 파일 이름({arm}, {seed})과 다릅니다"
        )
    entry = report_entry(report, arm, seed)
    if list(sidecar.get("features", [])) != list(entry["features"]):
        raise ModelRejected("실행기 요약의 피처 목록이 판정 리포트의 그 모델과 다릅니다")
    best = int(entry["best_iteration"])
    if int(sidecar.get("best_iteration", -1)) != best:
        raise ModelRejected(
            f"실행기 요약의 best_iteration {sidecar.get('best_iteration')}이 판정 리포트의 {best}와 다릅니다"
        )
    info = register_model.inspect_model(model_text)
    # 4번: 서빙 어댑터와 열이 다르면 여기서 멈춘다(register_model의 검사 그대로)
    register_model.check_against_adapter(info)
    if info["feature_names"] != list(entry["features"]):
        raise ModelRejected("모델 본문의 열 목록이 판정 리포트의 그 모델과 다릅니다")
    if info["num_trees"] != best:
        raise ModelRejected(
            f"모델 본문의 트리 {info['num_trees']}개가 best_iteration {best}과 다릅니다 - 판정에 쓴 모델과 반복 수가 다르다"
        )
    meta = report.get("meta", {})
    prereg = meta.get("preregistration", {})
    envs = meta.get("environments") or [{}]
    metrics = {
        "offline": {
            "label": meta.get("label"),
            "report": report_path,
            "report_sha256": report_sha256,
            "evidence_grade": meta.get("evidence", {}).get("grade"),
            "preregistration_commit": prereg.get("commit"),
            "preregistration_sha256": prereg.get("sha256"),
            "code_sha": meta.get("code_sha"),
            "arm": arm,
            "seed": seed,
            "role_in_adr": REGISTRABLE[arm],
            "best_iteration": best,
            "best_es_score": entry.get("best_es_score"),
            "trained_with": {k: envs[0].get(k) for k in ("lightgbm", "python", "system", "machine")},
        },
        "training_feature_definition_differs": training_definition_differences(),
        "license": LICENSE,
        "adr": ADR,
    }
    for key in ("masked_fit_request_share", "masked_es_request_share"):
        if key in entry:
            metrics["offline"][key] = entry[key]
    return register_model.build_row(
        model_text, f"ebnerd-v1.2-{arm}-s{seed}", name=name, role="shadow", metrics=metrics, source=source
    )


def _label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return path.name


def prepare(model_path: Path, report: dict, report_sha256: str, name: str, report_path: Path = REPORT) -> dict:
    """파일 하나를 읽어 검사하고 등록할 행을 만든다(DB 접속 없음)."""
    if not model_path.is_file():
        raise ModelRejected(f"모델 파일이 없습니다: {model_path}")
    check_location(model_path)
    arm, seed = arm_and_seed(model_path)
    sidecar_path = model_path.with_suffix(".json")
    if not sidecar_path.is_file():
        raise ModelRejected(f"실행기 요약이 없습니다: {sidecar_path.name}(모델 파일과 같은 디렉터리에 함께 옮기세요)")
    check_location(sidecar_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    return build_row(model_path.read_text(encoding="utf-8"), sidecar, report, arm, seed, name=name,
                     source=model_path.name, report_sha256=report_sha256, report_path=_label(report_path))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EB-NeRD M4 모델을 shadow로 등록한다 (ADR 0037)")
    ap.add_argument("--model", action="append", required=True,
                    help="<arm>_s<seed>.txt (옆에 같은 이름의 .json). 여러 번 주면 그 순서로 등록한다")
    ap.add_argument("--name", default="ranker", help="모델 이름(서빙의 RECSYS_MODEL_NAME, 기본 ranker)")
    ap.add_argument("--report", default=str(REPORT), help="판정 리포트 JSON(기본: 커밋된 M4 리포트)")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true", help="검사만 하고 등록하지 않는다(DB에 접속하지 않는다)")
    args = ap.parse_args(argv)

    report_bytes = Path(args.report).read_bytes()
    report = json.loads(report_bytes)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    rows: List[dict] = []
    try:
        # 전부 검사한 뒤에 등록한다: 둘째 파일이 어긋나 첫째만 올라가는 일이 없게
        rows = [prepare(Path(p), report, report_sha256, args.name, Path(args.report)) for p in args.model]
        if len({r["model_version"] for r in rows}) != len(rows):
            raise ModelRejected("같은 모델을 두 번 넘겼습니다")
        summaries = [{"name": r["model_name"], "version": r["model_version"], "role": r["role"],
                      "features": len(r["feature_names"]), "feature_schema_hash": r["feature_schema_hash"],
                      "model_sha256": r["metrics"]["registration"]["model_sha256"]} for r in rows]
        if args.dry_run:
            for s in summaries:
                print(json.dumps({"dry_run": True, **s}, ensure_ascii=False))
            return 0
        if not args.database_url:
            raise ModelRejected("DATABASE_URL(또는 --database-url)이 필요합니다.")
        import psycopg2

        conn = psycopg2.connect(args.database_url)
        try:
            for row, s in zip(rows, summaries):
                result = register_model.register(conn, row)
                print(json.dumps({**s, **result}, ensure_ascii=False))
        finally:
            conn.close()
    except ModelRejected as exc:
        print(f"등록하지 않음: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
