"""E15 실행 가능성 스모크: Colab GPU에서 NRMS-lite/SASRec-lite 1 epoch을 무작위 텐서로 돌려 시간·환경을 잰다.

EB-NeRD 데이터는 업로드하지 않는다. 텐서 크기만 ebnerd_demo(기사 11,777 / train 노출 24,724 / 노출당 후보
중앙값 8·p95 28 / 히스토리 중앙값 95)에 맞춘 무작위 값이라 지표는 무의미하고, 측정 대상은
(1) GPU 종류·torch·CUDA 버전, (2) P1형·P2형 학습 1 epoch 벽시계, (3) P2 전체 풀 채점 시간,
(4) 같은 seed 두 번 실행 시 손실이 비트 단위로 같은지(결정론)다.

    colab new -s e15smoke --gpu T4 && colab exec -s e15smoke -f e15_colab_smoke.py --timeout 900 && colab stop -s e15smoke
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")  # torch 결정론 모드 요구사항(CUDA 초기화 전에 설정)

import torch
import torch.nn as nn
import torch.nn.functional as F

# --- ebnerd_demo 형태 ---
N_ARTICLES = 11_777
EMB_DIM = 1024          # BGE-M3
N_IMPRESSIONS = 24_724  # demo train 노출 수
INVIEW_PAD = 30         # 노출당 후보 p95 28 → 30으로 패딩(마스크), 실제 하네스는 배치 내 최대 길이로 패딩
SEQ_LEN = 50            # 마지막 N 클릭
P2_NEG = 20             # poolneg와 같은 1 양성 + 20 풀 네거티브
P2_EVAL_REQUESTS = 20_000
P2_POOL = 235           # P2 평균 풀 크기
D_MODEL, N_HEADS, BATCH = 256, 4, 256
SMALL_OVER_DEMO = 129_080 / N_IMPRESSIONS  # ebnerd_small fit 창 노출 / demo train 노출


def seed_all(seed: int):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _sync(device: str):
    if device == "cuda":
        torch.cuda.synchronize()


class Projection(nn.Module):
    def __init__(self, table: torch.Tensor):
        super().__init__()
        self.table = nn.Embedding.from_pretrained(table, freeze=True, padding_idx=0)  # 고정 BGE-M3
        self.proj = nn.Sequential(nn.Linear(EMB_DIM, D_MODEL), nn.LayerNorm(D_MODEL), nn.Dropout(0.1))

    def forward(self, ids):
        return self.proj(self.table(ids).float())


class NRMSLite(nn.Module):
    """유저 인코더 = MHSA + additive attention over 마지막 N 클릭, 점수 = 유저·후보 내적."""

    def __init__(self, table):
        super().__init__()
        self.item = Projection(table)
        self.mha = nn.MultiheadAttention(D_MODEL, N_HEADS, dropout=0.1, batch_first=True)
        self.add_q = nn.Sequential(nn.Linear(D_MODEL, D_MODEL), nn.Tanh(), nn.Linear(D_MODEL, 1, bias=False))

    def user(self, hist, hist_mask):
        h = self.item(hist)
        a, _ = self.mha(h, h, h, key_padding_mask=~hist_mask, need_weights=False)
        w = self.add_q(a).squeeze(-1).masked_fill(~hist_mask, -1e4)
        w = torch.softmax(w, dim=1)
        return (w.unsqueeze(-1) * a).sum(1)

    def forward(self, hist, hist_mask, cands):
        u = self.user(hist, hist_mask)
        c = self.item(cands)
        return torch.einsum("bd,bkd->bk", u, c)


class SASRecLite(nn.Module):
    """인과 Transformer(위치 임베딩) over 클릭 시퀀스, 마지막 위치 표현 = 유저 벡터."""

    def __init__(self, table, n_layers=2):
        super().__init__()
        self.item = Projection(table)
        self.pos = nn.Embedding(SEQ_LEN, D_MODEL)
        layer = nn.TransformerEncoderLayer(D_MODEL, N_HEADS, dim_feedforward=4 * D_MODEL, dropout=0.1,
                                           batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, n_layers, enable_nested_tensor=False)
        self.register_buffer("causal", torch.triu(torch.ones(SEQ_LEN, SEQ_LEN, dtype=torch.bool), 1))

    def forward(self, hist, hist_mask, cands):
        h = self.item(hist) + self.pos(torch.arange(SEQ_LEN, device=hist.device))
        h = self.enc(h, mask=self.causal, src_key_padding_mask=~hist_mask)
        u = h[:, -1]  # 시퀀스는 오른쪽 정렬(최근 클릭이 마지막)
        return torch.einsum("bd,bkd->bk", u, self.item(cands))


def listwise_loss(logits, labels, cand_mask):
    """마스크된 softmax 교차 엔트로피(LambdaRank의 쿼리 그룹과 같은 단위). 다중 양성은 평균."""
    logits = logits.masked_fill(~cand_mask, -1e4)
    logp = torch.log_softmax(logits, dim=1)
    pos = labels & cand_mask
    return -(logp * pos).sum(1).div(pos.sum(1).clamp(min=1)).mean()


def make_batch(gen, b, n_cand, seq_len=SEQ_LEN, pad_prob=0.0, device="cuda"):
    hist = torch.randint(1, N_ARTICLES, (b, seq_len), generator=gen, device=device)
    hist_len = torch.randint(1, seq_len + 1, (b,), generator=gen, device=device)
    hist_mask = torch.arange(seq_len, device=device)[None, :] >= (seq_len - hist_len)[:, None]  # 오른쪽 정렬
    cands = torch.randint(1, N_ARTICLES, (b, n_cand), generator=gen, device=device)
    cand_len = torch.randint(2, n_cand + 1, (b,), generator=gen, device=device) if pad_prob else torch.full((b,), n_cand, device=device)
    cand_mask = torch.arange(n_cand, device=device)[None, :] < cand_len[:, None]
    labels = torch.zeros(b, n_cand, dtype=torch.bool, device=device)
    labels[:, 0] = True  # 1 양성(EB-NeRD 노출당 클릭 평균 1.01)
    return hist, hist_mask, cands, cand_mask, labels


def run_epoch(model, n_rows, n_cand, seed, pad_prob, device):
    seed_all(seed)
    gen = torch.Generator(device=device).manual_seed(seed)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    model.train()
    losses = []
    _sync(device)
    t0 = time.perf_counter()
    steps = (n_rows + BATCH - 1) // BATCH
    for _ in range(steps):
        hist, hm, cands, cm, y = make_batch(gen, BATCH, n_cand, pad_prob=pad_prob, device=device)
        loss = listwise_loss(model(hist, hm, cands), y, cm)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        losses.append(loss.detach())
    _sync(device)
    sec = time.perf_counter() - t0
    losses = torch.stack(losses).cpu()
    return {"steps": steps, "seconds": round(sec, 2), "first_loss": losses[0].item(), "last_loss": losses[-1].item(),
            "loss_sum_hex": losses.sum().item().hex()}


@torch.no_grad()
def score_full_pool(model, device):
    """P2 평가형: 20k 요청 × 235 후보 채점(그래디언트 없음)."""
    model.eval()
    gen = torch.Generator(device=device).manual_seed(123)
    _sync(device)
    t0 = time.perf_counter()
    out = 0.0
    for _ in range((P2_EVAL_REQUESTS + BATCH - 1) // BATCH):
        hist, hm, cands, cm, _ = make_batch(gen, BATCH, P2_POOL, device=device)
        out += model(hist, hm, cands).float().sum().item()
    _sync(device)
    return {"requests": P2_EVAL_REQUESTS, "pool": P2_POOL, "seconds": round(time.perf_counter() - t0, 2)}


def env_info() -> dict:
    info = {"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda,
            "cudnn": torch.backends.cudnn.version(), "cuda_available": torch.cuda.is_available(),
            "cpu_count": os.cpu_count(), "machine": platform.machine()}
    if torch.cuda.is_available():
        p = torch.cuda.get_device_properties(0)
        info.update({"gpu": p.name, "gpu_mem_gb": round(p.total_memory / 2**30, 1), "capability": f"{p.major}.{p.minor}"})
    try:
        info["nvidia_smi"] = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader"], text=True).strip()
    except Exception as e:  # noqa: BLE001
        info["nvidia_smi"] = f"unavailable: {e}"
    try:
        info["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError):
        pass
    for mod in ("numpy", "pandas", "lightgbm", "pyarrow"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            info[mod] = None
    return info


def main() -> int:
    t_all = time.perf_counter()
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    device = "cuda" if torch.cuda.is_available() else "cpu"
    res = {"env": env_info(), "shapes": {
        "n_articles": N_ARTICLES, "emb_dim": EMB_DIM, "train_impressions": N_IMPRESSIONS, "inview_pad": INVIEW_PAD,
        "seq_len": SEQ_LEN, "p2_neg": P2_NEG, "d_model": D_MODEL, "heads": N_HEADS, "batch": BATCH,
        "small_over_demo_fit_ratio": round(SMALL_OVER_DEMO, 2)}, "runs": {}}
    print(json.dumps({"env": res["env"]}), flush=True)
    seed_all(0)
    table = torch.randn(N_ARTICLES, EMB_DIM, generator=torch.Generator().manual_seed(0)).half()
    table = F.normalize(table.float(), dim=1).half()
    table[0] = 0

    for name, cls in (("nrms_lite", NRMSLite), ("sasrec_lite", SASRecLite)):
        # P1형(노출 재정렬): 후보 30 패딩, P2형(poolneg): 1+20
        for task, n_cand, pad in (("p1_inview", INVIEW_PAD, 1.0), ("p2_poolneg", 1 + P2_NEG, 0.0)):
            seed_all(0)
            model = cls(table).to(device)
            n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            warm = run_epoch(model, BATCH * 4, n_cand, 0, pad, device)  # 워밍업(커널 컴파일·할당) 별도 측정
            seed_all(0)
            model = cls(table).to(device)
            r = run_epoch(model, N_IMPRESSIONS, n_cand, 0, pad, device)
            seed_all(0)
            model2 = cls(table).to(device)
            r2 = run_epoch(model2, N_IMPRESSIONS, n_cand, 0, pad, device)
            r.update({"trainable_params": n_params, "warmup_seconds": warm["seconds"],
                      "deterministic_repeat_bitwise_equal": r["loss_sum_hex"] == r2["loss_sum_hex"],
                      "est_small_epoch_seconds": round(r["seconds"] * SMALL_OVER_DEMO, 1),
                      "peak_mem_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2) if device == "cuda" else None})
            res["runs"][f"{name}/{task}"] = r
            print(json.dumps({f"{name}/{task}": r}), flush=True)
            if task == "p2_poolneg":
                res["runs"][f"{name}/p2_score_full_pool"] = score_full_pool(model, device)
                print(json.dumps({f"{name}/p2_score_full_pool": res["runs"][f"{name}/p2_score_full_pool"]}), flush=True)
            del model, model2
            if device == "cuda":
                torch.cuda.empty_cache()
                torch.cuda.reset_peak_memory_stats()
    res["total_seconds"] = round(time.perf_counter() - t_all, 1)
    print("SMOKE_RESULT " + json.dumps(res), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
