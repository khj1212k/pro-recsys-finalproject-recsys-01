"""E0 사람 라벨 - 블라인드 내보내기와 라벨 지표 (ADR 0009 A8.8, docs/eval/labeling-guide.md).

내보내기(`export_blind`)는 `evaluation/llm/labeling_app`·`labels.LabelStore`가 읽는 형식 그대로 쓴다:
- clusters.jsonl: 처리 완료 eval 클러스터 전부의 기사(제목·본문 - ADR 0023 개정의 라벨링 입력). 예산·인프라로 중단된 것과
  미시작은 뺀다(A8.5 "집계와 라벨 대상에서 뺀다").
- outputs.jsonl: (i) 생성에 도달한 클러스터의 **첫 형식체 초안**(첫 generate 시도, 없거나 `_fallback`이면 빼고 건수를 적는다),
  (ii) 저장본의 형식체 초안이 첫 초안과 다르면(sha256) 그 **저장본**. 불투명 id·무작위 순서이고 첫 초안/저장본 구분,
  게이트·judge 결과는 숨긴다. 문체 변환본은 그 저장본에만 싣는다(tone_drift 라벨 대상).
- relabel.json: yaml `human_labels.relabel`(출력 5·클러스터 3·48시간, 축소안이면 3·2) - bake-off의 기본값 30/10이 아니다.
- retention.json: 라벨 마감 = 표본 중 가장 오래된 기사의 crawled_at + 30일(ADR 0023). 라벨링 UI는 마감 뒤에 열지 않고,
  `labels.LabelStore`가 열 때마다 마감이 지난 본문을 지운다.
후보 매핑(blind_key.json)은 run 디렉터리(data/experiments)에만 둔다.

    python -m evaluation.llm.e0_labels export-blind --run data/experiments/e0/<run> --export-dir data/exports/<T0>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evaluation.llm import e0_registered as reg
from evaluation.llm import frozen_export, labels as label_store

BLIND_KEY = "blind_key.json"


class ExportRefused(RuntimeError):
    pass


def _read(path: Path) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def labels_root() -> Path:
    return frozen_export.main_checkout_data() / "labels"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def processed_eval(items: Sequence[Dict[str, Any]], ends: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in sorted(items, key=lambda r: r["order"]) if r["split"] == "eval"
            and str(ends.get(r["item_id"], {}).get("outcome", "")).split(":")[0] in reg.NATURAL_OUTCOMES]


def export_blind(run_dir: Path, *, export_dir: Path, manifest_path: Path, prereg: Dict[str, Any],
                 labels_root: Optional[Path] = None, seed: Optional[int] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    from evaluation.llm import e0_sample

    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8")) \
        if (run_dir / "run_summary.json").exists() else None
    if not summary:
        raise ExportRefused("run이 끝나지 않았다(run_summary.json 없음)")
    if summary.get("void"):
        raise ExportRefused(f"void 실행에는 라벨을 달지 않는다(A8.6): {summary['void']}")
    if (run_dir / BLIND_KEY).exists():
        raise ExportRefused(f"{run_dir / BLIND_KEY}가 이미 있다 - 블라인드 id를 다시 만들면 라벨과 어긋난다")
    record = prereg.get("pre_run_record") or {}
    subset = (record.get("user_decisions") or {}).get("label_subset") or "full"
    relabel_spec = dict(reg.RELABEL) if subset == "full" else {**reg.RELABEL, **reg.RELABEL_REDUCED}

    items = e0_sample.read_manifest(manifest_path)
    ends = {r["item_id"]: r for r in _read(run_dir / "clusters.jsonl") if r["kind"] == "end"}
    processed = processed_eval(items, ends)
    if subset == "reduced_first10":
        first10 = {r["item_id"] for r in sorted((r for r in items if r["split"] == "eval"),
                                                 key=lambda r: r["order"])[:reg.REDUCED_FIRST_N]}
        output_items = [r for r in processed if r["item_id"] in first10]
    else:
        output_items = processed
    texts = _read(run_dir / "newsletters.jsonl")
    first: Dict[str, Dict[str, Any]] = {}
    saved: Dict[str, Dict[str, Any]] = {}
    for t in texts:
        if t.get("kind") == "draft_attempt" and t.get("attempt") == 1:
            first.setdefault(t["item_id"], t)
        if t.get("kind") == "saved_formal":
            saved[t["item_id"]] = t

    seed = secrets.randbits(63) if seed is None else int(seed)
    rng = random.Random(seed)
    outputs, key, excluded = [], {}, {"no_first_draft": [], "first_draft_fallback": []}
    used = set()

    def new_id() -> str:
        oid = f"o{rng.getrandbits(40):010x}"
        while oid in used:
            oid = f"o{rng.getrandbits(40):010x}"
        used.add(oid)
        return oid

    for r in output_items:
        f = first.get(r["item_id"])
        if f is None or not f.get("draft"):
            if any(t.get("item_id") == r["item_id"] and t.get("kind") == "draft_attempt" for t in texts) or \
                    r["item_id"] in saved:
                excluded["no_first_draft"].append(r["item_id"])
            continue
        if f.get("fallback"):
            excluded["first_draft_fallback"].append(r["item_id"])
        else:
            oid = new_id()
            key[oid] = {"item_id": r["item_id"], "kind": "first_draft", "attempt": 1, "sha256": f["sha256"]}
            outputs.append({"output_id": oid, "item_id": r["item_id"],
                            "draft": {k: f["draft"].get(k) or "" for k in ("title", "sentence", "content")},
                            "converted": {"title": "", "summary": "", "content": ""}})
        s = saved.get(r["item_id"])
        if s and s.get("formal_sha256") != f.get("sha256"):
            oid = new_id()
            conv = s.get("converted") or {}
            key[oid] = {"item_id": r["item_id"], "kind": "saved", "sha256": s["formal_sha256"],
                        "has_converted": bool(conv) and s.get("tone_fallback") != "formal",
                        "news_letter_id": s.get("news_letter_id")}
            outputs.append({"output_id": oid, "item_id": r["item_id"],
                            "draft": {k: (s.get("formal") or {}).get(k) or "" for k in ("title", "sentence", "content")},
                            "converted": ({k: conv.get(k) or "" for k in ("title", "summary", "content")}
                                          if key[oid]["has_converted"] else {"title": "", "summary": "", "content": ""})})
    rng.shuffle(outputs)

    export = frozen_export.load_export(Path(export_dir), now=now)  # 여는 김에 만료된 본문을 지운다
    rows = {int(r["id"]): r for r in export.rows}
    clusters, crawled = [], []
    for r in sorted(processed, key=lambda r: r["item_id"]):
        arts = []
        for a in r["articles"]:
            row = rows.get(int(a["raw_news_id"]))
            if row is None or row["body"] is None:
                raise ExportRefused(f"{r['item_id']}: 기사 {a['raw_news_id']}의 본문이 없다(보존 기한이 지났다) - 라벨 마감이 지났다")
            if row["content_sha256"] != a["body_sha256"]:
                raise ExportRefused(f"{r['item_id']}: 기사 {a['raw_news_id']}의 본문 해시가 표본 매니페스트와 다르다")
            arts.append({"raw_news_id": int(a["raw_news_id"]), "press_name": row["press"], "title": row["title"],
                         "body": row["body"], "url": row["url"]})
            crawled.append(row["crawled_at"])
        clusters.append({"item_id": r["item_id"], "articles": arts})
    sample_ids = {int(a["raw_news_id"]) for r in items for a in r["articles"]}
    oldest = min(rows[i]["crawled_at"] for i in sample_ids if i in rows)
    expires = frozen_export._parse(oldest) + timedelta(days=frozen_export.BODY_RETENTION_DAYS)

    item_ids = [c["item_id"] for c in clusters]
    relabel = {
        "outputs": sorted(rng.sample(sorted(key), min(int(relabel_spec["outputs"]), len(key)))),
        "clusters": sorted(rng.sample(item_ids, min(int(relabel_spec["clusters"]), len(item_ids)))),
        "min_hours_between_rounds": float(relabel_spec["min_hours_between_rounds"]),
    }
    root = Path(labels_root) if labels_root is not None else labels_root_default()
    target = frozen_export.ensure_under(root / f"{reg.PREREG_ID}-{run_dir.name}", root, what="라벨링 입력", strictly=True)
    if (target / "outputs.jsonl").exists():
        raise ExportRefused(f"{target}에 이미 내보낸 파일이 있다")
    old = os.umask(0o077)
    try:
        target.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    for name, data in (("outputs.jsonl", outputs), ("clusters.jsonl", clusters)):
        _write_private(target / name, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in data))
    _write_private(target / "relabel.json", json.dumps(relabel, ensure_ascii=False, indent=2))
    retention = {"expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "rule": "표본 중 가장 오래된 기사의 crawled_at + 30일(ADR 0023 개정, ADR 0009 A8.8)",
                 "export_identity_sha256": export.identity_sha256, "prereg": reg.PREREG_ID, "run": run_dir.name}
    _write_private(target / label_store.RETENTION_FILE, json.dumps(retention, ensure_ascii=False, indent=2))
    _write_private(run_dir / BLIND_KEY, json.dumps({"seed": seed, "outputs": key, "excluded": excluded,
                                                    "label_subset": subset, "labels_dir": str(target)},
                                                   ensure_ascii=False, indent=2))
    return {"dir": str(target), "outputs": len(outputs), "clusters": len(clusters), "relabel": relabel,
            "excluded": {k: len(v) for k, v in excluded.items()}, "expires_at_utc": retention["expires_at_utc"],
            "label_subset": subset}


def labels_root_default() -> Path:
    return labels_root()


# ---------------------------------------------------------------- 라벨 지표 (A8.8 쓰임)


def _overlap(a: Tuple[str, int, int], b: Tuple[str, int, int]) -> bool:
    return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]


def label_metrics(labels_dir: Path, run_dir: Path, items: Sequence[Dict[str, Any]], ends: Dict[str, Dict[str, Any]],
                  record: Dict[str, Any]) -> Dict[str, Any]:
    from evaluation.llm.e0_report import ratio

    labels_dir, run_dir = Path(labels_dir), Path(run_dir)
    key_doc = json.loads((run_dir / BLIND_KEY).read_text(encoding="utf-8")) if (run_dir / BLIND_KEY).exists() else None
    if not key_doc or not (labels_dir / "outputs.jsonl").exists():
        return {"status": "pending"}
    data = label_store.load_labels(labels_dir)
    out1, cl1 = data["output"][1], data["cluster"][1]
    if not out1 and not cl1:
        return {"status": "pending"}
    key = key_doc["outputs"]
    processed = processed_eval(items, ends)
    n_p = len(processed)
    by_item: Dict[str, Dict[str, str]] = {}
    for oid, k in key.items():
        by_item.setdefault(k["item_id"], {})[k["kind"]] = oid
    saved_items = {r["item_id"] for r in processed if ends[r["item_id"]]["outcome"] == "completed"}

    def saved_label(item_id: str) -> Optional[Dict[str, Any]]:
        oids = by_item.get(item_id, {})
        oid = oids.get("saved") or (oids.get("first_draft") if item_id in saved_items else None)
        return out1.get(oid) if oid else None

    primary_y = sum(1 for i in saved_items if (saved_label(i) or {}).get("publishable") is True)
    saved_labeled = [saved_label(i) for i in saved_items if saved_label(i) is not None]
    first_labels = [out1[oid] for item_id, o in by_item.items() if (oid := o.get("first_draft")) and oid in out1]
    res: Dict[str, Any] = {
        "status": "partial" if len(out1) < len(key) or len(cl1) < n_p else "complete",
        "labeled": {"outputs": len(out1), "outputs_total": len(key), "clusters": len(cl1), "clusters_total": n_p},
        "publishable_primary": ratio(primary_y, n_p),
        "publishable_saved_only": ratio(sum(1 for l in saved_labeled if l.get("publishable")), len(saved_labeled)),
        "publishable_first_draft": ratio(sum(1 for l in first_labels if l.get("publishable")), len(first_labels)),
    }
    table: Dict[str, Dict[str, int]] = {}
    for r in processed:
        outcome = ends[r["item_id"]]["outcome"].split(":")[0]
        cat = "saved" if outcome == "completed" else outcome
        lab = cl1.get(r["item_id"])
        if lab is None:
            continue
        col = "single_event_Y" if lab.get("single_event") else "single_event_N"
        table.setdefault(cat, {"single_event_Y": 0, "single_event_N": 0})[col] += 1
    res["outcome_by_single_event"] = table

    # 결정론 검사기 대 사람(첫 초안)
    nodes = _read(run_dir / "nodes.jsonl")
    first_check = {}
    last_check = {}
    for n in nodes:
        if n["node"] == "check_faithfulness" and n.get("reason") != "generator_fallback":
            if n.get("attempt") == 1:
                first_check.setdefault(n["item_id"], n)
            last_check[n["item_id"]] = n
    checker_items = human_spans = 0
    checker_hit = 0
    recall = {"number": [0, 0], "quote": [0, 0]}
    two_by_two = {"blocked_and_error": 0, "blocked_no_error": 0, "passed_and_error": 0, "passed_no_error": 0}
    for item_id, oids in by_item.items():
        oid = oids.get("first_draft")
        lab = out1.get(oid) if oid else None
        check = first_check.get(item_id)
        if lab is None or check is None:
            continue
        checks = [(s["field"], s["start"], s["end"]) for s in check.get("spans") or []
                  if s["group"] == "blocking" and s.get("field") is not None and s.get("start") is not None]
        humans = [((e["field"], e["start"], e["end"]), e["type"]) for e in lab.get("fact_errors") or []]
        checker_items += len(checks)
        checker_hit += sum(1 for c in checks if any(_overlap(c, h[0]) for h in humans))
        for kind in ("number", "quote"):
            spans = [h[0] for h in humans if h[1] == kind]
            recall[kind][1] += len(spans)
            recall[kind][0] += sum(1 for h in spans if any(_overlap(h, c) for c in checks))
        blocked = sum((check.get("blocking") or {}).values()) > 0
        error = any(h[1] in ("number", "quote") for h in humans)
        two_by_two[f"{'blocked' if blocked else 'passed'}_{'and_error' if error else 'no_error'}"] += 1
        human_spans += len(humans)
    res["checker_vs_human_first_draft"] = {
        "precision": ratio(checker_hit, checker_items),
        "recall_number": ratio(*recall["number"]), "recall_quote": ratio(*recall["quote"]),
        "draft_level_2x2": two_by_two,
        "computed_values_counted_as_fp": None,
        "note": "가이드 2.1의 '원문 수치로 정확히 계산되는 값'은 라벨에 따로 표시하는 칸이 없어 자동으로 가르지 못한다 - "
                "검사기 오탐 안에 섞여 있고, 건수는 사람이 오탐 항목을 다시 보고 적는다.",
    }
    gate_miss = 0
    adv_items = adv_hit = 0
    for item_id in saved_items:
        lab = saved_label(item_id)
        if lab is None:
            continue
        humans = [(e["field"], e["start"], e["end"]) for e in lab.get("fact_errors") or []]
        gate_miss += sum(1 for e in lab.get("fact_errors") or [] if e["type"] in ("number", "quote"))
        check = last_check.get(item_id) or {}
        adv = [(s["field"], s["start"], s["end"]) for s in check.get("spans") or []
               if s["group"] == "advisory" and s["type"] == "entities" and s.get("field") is not None]
        adv_items += len(adv)
        adv_hit += sum(1 for a in adv if any(_overlap(a, h) for h in humans))
    res["saved"] = {"gate_miss_number_quote_errors": gate_miss, "advisory_entity_precision": ratio(adv_hit, adv_items)}

    # judge shadow 대 publishable(첫 초안의 judge 판정)
    judge = {}
    for n in nodes:
        if n["node"] == "eval_newsletter" and n.get("attempt") == 1 and n.get("criteria"):
            judge.setdefault(n["item_id"], n)
    jt = {"judge_pass_pub_Y": 0, "judge_pass_pub_N": 0, "judge_fail_pub_Y": 0, "judge_fail_pub_N": 0}
    for item_id, oids in by_item.items():
        lab = out1.get(oids.get("first_draft") or "")
        j = judge.get(item_id)
        if lab is None or j is None:
            continue
        jt[f"judge_{'pass' if j.get('decision') == 'PASS' else 'fail'}_pub_{'Y' if lab.get('publishable') else 'N'}"] += 1
    agree = jt["judge_pass_pub_Y"] + jt["judge_fail_pub_N"]
    res["judge_shadow_vs_publishable"] = {"table": jt, "agreement": ratio(agree, sum(jt.values())),
                                          "note": "κ는 참고로만(A2), n이 작아 선정에 쓰지 않는다"}
    covered, total = 0, 0
    types: Dict[str, int] = {}
    for oid, lab in out1.items():
        item_id = key.get(oid, {}).get("item_id")
        facts = (cl1.get(item_id) or {}).get("key_facts") or []
        total += len(facts)
        covered += len(lab.get("key_facts_covered") or [])
        for e in lab.get("fact_errors") or []:
            types[e["type"]] = types.get(e["type"], 0) + 1
    res["key_fact_coverage"] = ratio(covered, total)
    res["fact_error_types"] = dict(sorted(types.items()))

    # 재라벨 일치율
    out2, cl2 = data["output"][2], data["cluster"][2]
    out_pairs = [(oid, out1[oid], out2[oid]) for oid in out2 if oid in out1]
    cl_pairs = [(i, cl1[i], cl2[i]) for i in cl2 if i in cl1]
    res["relabel"] = {
        "outputs_publishable_agreement": ratio(sum(1 for _, a, b in out_pairs if a.get("publishable") == b.get("publishable")),
                                               len(out_pairs)),
        "clusters_single_event_agreement": ratio(sum(1 for _, a, b in cl_pairs
                                                     if a.get("single_event") == b.get("single_event")), len(cl_pairs)),
        "disagreement_ids": sorted([o for o, a, b in out_pairs if a.get("publishable") != b.get("publishable")] +
                                   [i for i, a, b in cl_pairs if a.get("single_event") != b.get("single_event")]),
        "kappa": "참고로만(정의되면) - 해석하지 않는다",
    }
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E0 블라인드 내보내기(ADR 0009 A8.8)")
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export-blind")
    ex.add_argument("--run", required=True, help="data/experiments/e0/<run>")
    ex.add_argument("--export-dir", required=True)
    ex.add_argument("--prereg", default=str(reg.PREREG_PATH))
    ex.add_argument("--manifest", default=str(reg.REPO_ROOT / reg.SAMPLE_MANIFEST_REL))
    args = parser.parse_args(argv)
    try:
        out = export_blind(Path(args.run), export_dir=Path(args.export_dir), manifest_path=Path(args.manifest),
                           prereg=reg.load_prereg(Path(args.prereg)))
    except (ExportRefused, frozen_export.ExportError) as e:
        print(f"거절: {e}")
        return 2
    print(json.dumps({k: v for k, v in out.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
