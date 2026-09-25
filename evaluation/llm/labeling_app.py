"""로컬 라벨링 UI (FastAPI + HTML 한 장, 빌드 단계 없음) - ADR 0009, docs/eval/labeling-guide.md.

  python -m evaluation.llm.labeling_app --run bakeoff-v1   # http://127.0.0.1:8765
  재라벨(intra-rater, 2차)은 같은 서버에서 http://127.0.0.1:8765/?round=2

127.0.0.1에만 바인딩한다 - 기사 원문을 담고 있어 외부에 열면 안 된다.
UI는 data/labels/<run>/만 읽는다. 후보 매핑(data/bakeoff/<run>/blind_key.json)과 자동 지표는
API가 절대 돌려주지 않는다. 2차 라벨 화면에는 1차 라벨을 보여 주지 않는다.
"""

import argparse
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from evaluation.llm.labels import LabelError, LabelStore

INDEX_HTML = Path(__file__).resolve().parent / "labeling" / "index.html"
LABELS_ROOT = Path(__file__).resolve().parents[2] / "data" / "labels"


def create_app(labels_dir: Path, now=None) -> FastAPI:
    app = FastAPI(title="newsletter labeling", docs_url=None, redoc_url=None, openapi_url=None)

    def store() -> LabelStore:
        # 요청마다 새로 읽는다: 라벨 파일은 작고, 다른 탭/재시작 사이의 상태 불일치를 없앤다
        return LabelStore(labels_dir, now=now) if now else LabelStore(labels_dir)

    def task_payload(s: LabelStore, kind: str, target_id: str, round_: int) -> dict:
        existing = s.latest(kind, round_).get(target_id)
        base = {"kind": kind, "target_id": target_id, "round": round_,
                "existing": existing["label"] if existing else None}
        if kind == "cluster":
            if target_id not in s.clusters:
                raise HTTPException(404, "없는 클러스터")
            return {**base, "articles": s.clusters[target_id]["articles"]}
        out = s.outputs_by_id.get(target_id)
        if out is None:
            raise HTTPException(404, "없는 출력")
        return {**base, "item_id": out["item_id"], "draft": out["draft"], "converted": out["converted"],
                "articles": s.clusters.get(out["item_id"], {}).get("articles", []),
                "key_facts": s.cluster_key_facts(out["item_id"])}

    @app.get("/")
    def index():
        return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8")

    @app.get("/api/progress")
    def progress(round: int = Query(1)):
        return store().progress(round)

    @app.get("/api/next")
    def next_task(round: int = Query(1)):
        s = store()
        t = s.next_task(round)
        return {"task": task_payload(s, t["kind"], t["target_id"], round) if t else None,
                "progress": s.progress(round)}

    @app.get("/api/task/{kind}/{target_id}")
    def get_task(kind: str, target_id: str, round: int = Query(1)):
        s = store()
        if kind not in ("cluster", "output"):
            raise HTTPException(404, "없는 종류")
        return task_payload(s, kind, target_id, round)

    @app.post("/api/labels/{kind}/{target_id}")
    def save_label(kind: str, target_id: str, round: int = Query(1), payload: dict = Body(...)):
        try:
            store().save(kind, target_id, round, payload)
        except LabelError as e:
            raise HTTPException(422, str(e))
        return next_task(round)

    return app


def main(argv: Optional[list] = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="data/labels/<run>")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    labels_dir = LABELS_ROOT / args.run
    if not (labels_dir / "outputs.jsonl").exists():
        parser.error(f"{labels_dir}/outputs.jsonl이 없습니다 - 먼저 bakeoff export-blind를 실행하세요")
    uvicorn.run(create_app(labels_dir), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
