"""E15 신경망 arm의 학습·채점·결정론 게이트 (ADR 0013 A3.4·A3.9). torch가 필요하다.

- fp32 고정(AMP·fp16·TF32 없음), AdamW, grad-norm clip. 초기화·드롭아웃은 torch seed, 배치 순서는 (seed, epoch)로 고정.
- 조기 종료 지표는 호출부가 함수로 준다(es 행의 nDCG@10). 블록 모델처럼 epoch 수가 정해진 학습은 fixed_epochs로 돌리고
  es를 보지 않는다.
- 채점은 과제의 후보 쌍 순서 그대로의 1차원 배열을 돌려준다(LightGBM의 predict와 같은 모양).
"""
from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

# CUDA의 결정론적 행렬곱에 필요하다. CUDA가 초기화되기 전에 있어야 하므로 torch를 임포트하기 전에 둔다.
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import numpy as np  # noqa: E402
import torch  # noqa: E402

from .datasets import NeuralInputs, collate, eval_batches, group_batches  # noqa: E402
from .models import NeuralRanker, NeuralSpec, listwise_loss, next_click_loss  # noqa: E402


def deterministic_setup(seed: int) -> None:
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


def resolve_device(name: str = "auto") -> torch.device:
    if name == "auto":
        name = "cuda" if torch.cuda.is_available() else "cpu"
    return torch.device(name)


def device_fingerprint(device: torch.device) -> dict:
    """신경망 단위를 이어서 돌려도 되는지 가르는 기록: torch 버전과 장치가 같아야 한다."""
    out = {"torch": torch.__version__, "device": device.type, "gpu": None, "cuda": torch.version.cuda,
           "cudnn": torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else None}
    if device.type == "cuda":
        out["gpu"] = torch.cuda.get_device_name(device)
    return out


def emb_tensor(emb: np.ndarray, device: torch.device) -> torch.Tensor:
    """고정 기사 벡터 + 맨 끝의 패딩용 0 행. 학습되지 않는다."""
    e = torch.zeros((emb.shape[0] + 1, emb.shape[1]), dtype=torch.float32)
    e[:-1] = torch.from_numpy(np.ascontiguousarray(emb, dtype=np.float32))
    return e.to(device)


def _to_torch(batch: Mapping[str, np.ndarray], device: torch.device) -> dict:
    return {k: torch.from_numpy(v).to(device) for k, v in batch.items() if k != "pair_index"}


@dataclass
class TrainedNeural:
    spec: NeuralSpec
    seed: int
    state: dict
    best_epoch: int
    n_cont: int
    n_categories: int
    es_curve: list = field(default_factory=list)
    loss_curve: list = field(default_factory=list)
    seconds: float = 0.0
    n_params: int = 0

    def summary(self) -> dict:
        return {"family": self.spec.family, "spec": self.spec.to_dict(), "seed": self.seed, "best_epoch": self.best_epoch,
                "epochs_run": len(self.loss_curve), "es_curve": [round(float(x), 6) for x in self.es_curve],
                "best_es_score": max(self.es_curve) if self.es_curve else None, "train_seconds": round(self.seconds, 1),
                "n_params": self.n_params, "n_cont": self.n_cont, "n_categories": self.n_categories}


def build_model(spec: NeuralSpec, emb_dim: int, n_cont: int, n_categories: int, fixed: Mapping) -> NeuralRanker:
    return NeuralRanker(spec, emb_dim, n_cont, n_categories, fixed)


def _score(model: NeuralRanker, inp: NeuralInputs, emb_t: torch.Tensor, device: torch.device, max_pairs: int) -> np.ndarray:
    out = np.zeros(len(inp.cand_item), dtype=np.float32)
    model.eval()
    with torch.no_grad():
        for groups in eval_batches(inp.n_candidates, max_pairs):
            batch = collate(inp, groups)
            scores, _, _ = model(emb_t, _to_torch(batch, device))
            mask = batch["cand_mask"]
            out[batch["pair_index"][mask]] = scores.cpu().numpy()[mask]
    return out


def fit(spec: NeuralSpec, fit_in: NeuralInputs, emb: np.ndarray, *, seed: int, device: torch.device, fixed: Mapping,
        n_categories: int, max_epochs: int, patience: int = 2, es_in: Optional[NeuralInputs] = None,
        es_metric: Optional[Callable[[np.ndarray], float]] = None, fixed_epochs: Optional[int] = None,
        max_pairs: int = 65536) -> TrainedNeural:
    """학습. fixed_epochs가 있으면 정확히 그만큼 돌고 es를 보지 않는다. 없으면 es 지표가 patience번 연속으로 나아지지
    않을 때 멈추고 가장 좋았던 epoch의 가중치를 돌려준다."""
    if fixed_epochs is None and (es_in is None or es_metric is None):
        raise ValueError("조기 종료에는 es 입력과 지표 함수가 필요합니다")
    t0 = time.time()
    deterministic_setup(seed)
    n_cont = int(fit_in.cont.shape[1])
    model = build_model(spec, emb.shape[1], n_cont, n_categories, fixed).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=spec.lr, weight_decay=spec.weight_decay)
    emb_t = emb_tensor(emb, device)
    use_aux = spec.family == "sasrec" and spec.aux_lambda > 0
    best_state, best_metric, best_epoch, bad = None, -math.inf, 0, 0
    es_curve, loss_curve = [], []
    for epoch in range(1, int(fixed_epochs or max_epochs) + 1):
        model.train()
        total = 0.0
        for groups in group_batches(fit_in.n_candidates, spec.batch, seed, epoch):
            batch = _to_torch(collate(fit_in, groups, with_aux=use_aux), device)
            scores, h, seq_proj = model(emb_t, batch)
            loss = listwise_loss(scores, batch["labels"], batch["cand_mask"])
            if use_aux:
                loss = loss + spec.aux_lambda * next_click_loss(model, emb_t, h, seq_proj, batch["seq_mask"],
                                                                batch["aux_neg"])
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), float(fixed["grad_clip"]))
            opt.step()
            total += float(loss.detach())
        loss_curve.append(total)
        if fixed_epochs is not None:
            continue
        metric = float(es_metric(_score(model, es_in, emb_t, device, max_pairs)))
        es_curve.append(metric)
        if not math.isnan(metric) and metric > best_metric:
            best_metric, best_epoch, bad = metric, epoch, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    if best_state is None:
        best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        best_epoch = len(loss_curve)
    return TrainedNeural(spec=spec, seed=seed, state=best_state, best_epoch=best_epoch, n_cont=n_cont,
                         n_categories=n_categories, es_curve=es_curve, loss_curve=loss_curve, seconds=time.time() - t0,
                         n_params=sum(p.numel() for p in model.parameters()))


def predict(trained: TrainedNeural, inp: NeuralInputs, emb: np.ndarray, *, device: torch.device, fixed: Mapping,
            max_pairs: int = 65536) -> np.ndarray:
    """후보 쌍 순서 그대로의 점수 [쌍 수] float32."""
    if inp.cont.shape[1] != trained.n_cont:
        raise ValueError("스칼라 블록의 폭이 학습 때와 다릅니다(다른 통계로 만든 입력)")
    model = build_model(trained.spec, emb.shape[1], trained.n_cont, trained.n_categories, fixed)
    model.load_state_dict(trained.state)
    return _score(model.to(device), inp, emb_tensor(emb, device), device, max_pairs)


def determinism_gate(spec: NeuralSpec, fit_in: NeuralInputs, es_in: NeuralInputs, emb: np.ndarray, *,
                     device: torch.device, fixed: Mapping, n_categories: int, epochs: int = 1, repeats: int = 2,
                     seed: int = 0) -> dict:
    """같은 seed로 실제 학습 경로를 repeats번 돌려 손실 합과 es 점수 배열의 해시가 같은지 본다."""
    runs = []
    for _ in range(repeats):
        t = fit(spec, fit_in, emb, seed=seed, device=device, fixed=fixed, n_categories=n_categories, max_epochs=epochs,
                fixed_epochs=epochs)
        s = predict(t, es_in, emb, device=device, fixed=fixed)
        runs.append({"loss_sum": repr(float(sum(t.loss_curve))), "es_scores_sha256": hashlib.sha256(s.tobytes()).hexdigest()})
    return {"family": spec.family, "spec": spec.to_dict(), "runs": runs, "fit_groups": fit_in.n_groups,
            "status": "pass" if all(r == runs[0] for r in runs) else "fail"}


def save_trained(path: Path, trained: TrainedNeural, extra: Optional[dict] = None) -> None:
    """가중치와 그 모델의 표준화 통계 등(extra)을 한 파일로. 임시 이름으로 쓴 뒤 바꿔 넣는다."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    torch.save({"spec": trained.spec.to_dict(), "seed": trained.seed, "state": trained.state,
                "best_epoch": trained.best_epoch, "n_cont": trained.n_cont, "n_categories": trained.n_categories,
                "summary": trained.summary(), "extra": extra or {}}, tmp)
    os.replace(tmp, path)


def load_trained(path: Path) -> tuple[TrainedNeural, dict]:
    blob = torch.load(Path(path), map_location="cpu", weights_only=True)
    s = blob["summary"]
    t = TrainedNeural(spec=NeuralSpec(**blob["spec"]), seed=int(blob["seed"]), state=blob["state"],
                      best_epoch=int(blob["best_epoch"]), n_cont=int(blob["n_cont"]), n_categories=int(blob["n_categories"]),
                      es_curve=list(s.get("es_curve", [])), seconds=float(s.get("train_seconds", 0.0)),
                      n_params=int(s.get("n_params", 0)))
    return t, blob["extra"]
