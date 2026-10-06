#!/usr/bin/env python3
"""LightGBM text 모델을 model_registry에 등록한다 (ADR 0033).

    DATABASE_URL=postgresql://... python scripts/register_model.py \\
        --model runs/ebnerd_v1_2_cold/models/poolneg_seed0.txt --version ebnerd-poolneg-s0 \\
        [--name ranker] [--role shadow] [--metrics metrics.json] [--note "..."]

등록하는 것: 모델 본문, 버전, 역할, 모델이 학습한 열 이름(순서 포함), 서빙 피처 스키마의 지문.

**거절하는 모델**: 모델 파일의 열 목록이 서빙 어댑터(recsys_core.serving.FEATURE_NAMES)와 이름·순서까지 같지
않은 모델. LightGBM은 열 이름이 아니라 열 위치로 예측하므로, 순서만 달라도 서빙은 엉뚱한 값을 엉뚱한 자리에
넣게 된다. 서빙도 요청마다 같은 비교를 하지만(app/recsys/lgbm_scorer.py) 그때는 점수가 조용히 안 남을 뿐이라,
등록 시점에 이유와 함께 막는다. 열 이름이 없는 모델(Column_0 ...)도 같은 이유로 거절한다.

스키마 지문: 이름은 같아도 정의(창 길이, 반감기, 단기 상한 등)가 바뀌면 지문이 바뀐다. 등록할 때의 지문을 함께
적어 두면, 서빙의 정의가 달라진 뒤에 이 모델이 점수를 내는 일이 없다(scorer.schema_mismatch).

역할:
- shadow(기본): 같은 후보에 점수만 매겨 칸 로그에 남긴다. 응답은 바뀌지 않는다. 서빙은 이름별로 최신
  RECSYS_SHADOW_MAX개만 읽는다.
- active: 목록을 만드는 모델. 이름마다 하나뿐이라, 지금의 활성 모델은 같은 트랜잭션에서 retired로 바뀐다.
  응답이 바뀌는 일이므로 --yes-change-responses를 같이 줘야 한다.

같은 (이름, 버전)이 이미 있으면 실패한다. 등록된 모델을 바꾸지 않는다 - 새 버전으로 등록한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from recsys_core import serving  # noqa: E402

ROLES = ("shadow", "active")
MAX_NAME_LEN = 64  # model_registry.model_name / model_version


class ModelRejected(ValueError):
    """모델을 등록하지 않는다. 메시지에 이유가 있다."""


def describe_feature_mismatch(model_names: Sequence[str], adapter_names: Sequence[str]) -> Optional[str]:
    """모델의 열 목록이 어댑터와 다르면 무엇이 다른지, 같으면 None."""
    model_names, adapter_names = list(model_names), list(adapter_names)
    if model_names == adapter_names:
        return None
    missing = [n for n in adapter_names if n not in model_names]
    extra = [n for n in model_names if n not in adapter_names]
    parts = [f"모델 {len(model_names)}열, 서빙 어댑터 {len(adapter_names)}열"]
    if missing:
        parts.append(f"모델에 없는 열: {missing}")
    if extra:
        parts.append(f"어댑터에 없는 열: {extra}")
    if not missing and not extra:
        first = next(i for i, (a, b) in enumerate(zip(model_names, adapter_names)) if a != b)
        parts.append(f"순서가 다름: {first}번째가 모델 '{model_names[first]}', 어댑터 '{adapter_names[first]}'")
    return "; ".join(parts)


def inspect_model(model_text: str) -> dict:
    """모델 본문을 LightGBM으로 읽어 열 이름과 크기를 돌려준다. 읽을 수 없으면 ModelRejected."""
    import lightgbm as lgb

    try:
        booster = lgb.Booster(model_str=model_text)
    except Exception as exc:
        raise ModelRejected(f"LightGBM text 모델로 읽을 수 없습니다: {exc}") from exc
    return {
        "feature_names": list(booster.feature_name()),
        "num_features": int(booster.num_feature()),
        "num_trees": int(booster.num_trees()),
    }


def check_against_adapter(info: dict) -> None:
    problem = describe_feature_mismatch(info["feature_names"], serving.FEATURE_NAMES)
    if problem:
        raise ModelRejected(
            "모델의 피처 목록이 서빙 어댑터와 다릅니다 - " + problem
            + ". 이 모델은 서빙 피처로 채점할 수 없습니다(recsys_core.serving.FEATURE_NAMES와 같은 열·같은 순서로 학습해야 합니다)."
        )


def build_row(model_text: str, version: str, name: str = "ranker", role: str = "shadow",
              metrics: Optional[dict] = None, source: Optional[str] = None) -> dict:
    """등록할 행. 검증을 통과하지 못하면 ModelRejected."""
    if role not in ROLES:
        raise ModelRejected(f"role은 {ROLES} 중 하나여야 합니다: {role!r}")
    for label, value in (("name", name), ("version", version)):
        if not value or len(value) > MAX_NAME_LEN:
            raise ModelRejected(f"{label}은 1~{MAX_NAME_LEN}자여야 합니다: {value!r}")
    info = inspect_model(model_text)
    check_against_adapter(info)
    provenance = {
        "model_sha256": hashlib.sha256(model_text.encode("utf-8")).hexdigest(),
        "num_trees": info["num_trees"],
        "feature_schema_version": serving.FEATURE_SCHEMA_VERSION,
        "registered_with": "scripts/register_model.py",
    }
    if source:
        provenance["source_file"] = source
    return {
        "model_name": name,
        "model_version": version,
        "model_format": "lightgbm_text",
        "model_text": model_text,
        "feature_names": info["feature_names"],
        "feature_schema_hash": serving.SCHEMA_HASH,
        "metrics": {**(metrics or {}), "registration": provenance},
        "role": role,
        "is_active": role == "active",
    }


def register(conn, row: dict) -> dict:
    """한 트랜잭션으로 등록한다. conn은 psycopg2 연결이다. 돌려주는 것: 등록된 model_id와, active로 등록했을 때
    retired로 바뀐 이전 활성 버전들."""
    import psycopg2
    from psycopg2.extras import Json

    if conn.autocommit:
        # 활성 모델을 바꾸는 두 문장(이전 활성 내리기, 새 행 넣기)이 따로 커밋되면 중간에 실패했을 때 활성 모델이 없다.
        raise ValueError("register()에는 autocommit이 꺼진 연결이 필요합니다")
    retired: List[str] = []
    try:
        with conn, conn.cursor() as cur:
            if row["role"] == "active":
                cur.execute(
                    "UPDATE model_registry SET role = 'retired', is_active = false "
                    "WHERE model_name = %s AND is_active RETURNING model_version",
                    (row["model_name"],),
                )
                retired = [r[0] for r in cur.fetchall()]
            cur.execute(
                "INSERT INTO model_registry (model_name, model_version, model_format, model_text, feature_names, "
                "feature_schema_hash, metrics, is_active, role) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING model_id",
                (row["model_name"], row["model_version"], row["model_format"], row["model_text"],
                 Json(row["feature_names"]), row["feature_schema_hash"], Json(row["metrics"]), row["is_active"],
                 row["role"]),
            )
            model_id = cur.fetchone()[0]
    except psycopg2.errors.UniqueViolation as exc:
        raise ModelRejected(
            f"{row['model_name']}@{row['model_version']}은 이미 등록돼 있습니다. 등록된 모델은 바꾸지 않습니다 - "
            "새 버전 이름으로 등록하세요."
        ) from exc
    return {"model_id": model_id, "retired_versions": retired}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LightGBM text 모델을 model_registry에 등록한다 (ADR 0033)")
    ap.add_argument("--model", required=True, help="LightGBM text 모델 파일(booster.save_model의 출력)")
    ap.add_argument("--version", required=True, help="등록할 버전 이름(이름별로 유일)")
    ap.add_argument("--name", default="ranker", help="모델 이름(서빙의 RECSYS_MODEL_NAME, 기본 ranker)")
    ap.add_argument("--role", choices=ROLES, default="shadow")
    ap.add_argument("--metrics", help="같이 남길 오프라인 지표 JSON 파일(선택)")
    ap.add_argument("--note", help="같이 남길 메모(선택)")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--yes-change-responses", action="store_true",
                    help="--role active에 필요: 이 모델이 목록을 만들기 시작한다는 것을 확인한다")
    ap.add_argument("--dry-run", action="store_true", help="검증만 하고 등록하지 않는다(DB에 접속하지 않는다)")
    args = ap.parse_args(argv)

    path = Path(args.model)
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8")) if args.metrics else {}
    if args.note:
        metrics["note"] = args.note
    try:
        if args.role == "active" and not args.yes_change_responses:
            raise ModelRejected("--role active는 응답을 바꿉니다. 확인하려면 --yes-change-responses를 같이 주세요.")
        row = build_row(path.read_text(encoding="utf-8"), args.version, name=args.name, role=args.role,
                        metrics=metrics, source=path.name)
        summary = {"name": row["model_name"], "version": row["model_version"], "role": row["role"],
                   "features": len(row["feature_names"]), "feature_schema_hash": row["feature_schema_hash"],
                   "model_sha256": row["metrics"]["registration"]["model_sha256"]}
        if args.dry_run:
            print(json.dumps({"dry_run": True, **summary}, ensure_ascii=False))
            return 0
        if not args.database_url:
            raise ModelRejected("DATABASE_URL(또는 --database-url)이 필요합니다.")
        import psycopg2

        conn = psycopg2.connect(args.database_url)
        try:
            result = register(conn, row)
        finally:
            conn.close()
    except ModelRejected as exc:
        print(f"등록하지 않음: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**summary, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
