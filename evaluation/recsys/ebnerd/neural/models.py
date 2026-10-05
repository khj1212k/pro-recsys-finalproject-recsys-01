"""E15의 신경망 arm: NRMS-lite, SASRec-lite, 후기 융합 헤드, 손실 (ADR 0013 A3.4). torch가 필요하다.

공통 구조: 고정 기사 벡터 -> 학습 투영(히스토리와 후보가 공유) -> 유저 인코더 -> u,
점수 s = MLP2([x̃, <u,c>/sqrt(d), W_p(u*c)]). 빈 시퀀스는 u = 0으로 고정한다(학습되는 빈 시퀀스 벡터가 없다).
원 논문과 다른 점: 뉴스 인코더가 고정 임베딩의 투영이고, 스칼라 블록 x̃가 헤드에 들어가고, 손실이 노출 단위
그룹 전체 softmax다. 폭·차원 같은 고정값은 사전 등록 yaml의 neural.fixed에서 받는다.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping, Optional

import torch
import torch.nn.functional as F
from torch import nn

from .sequences import GENDER_TOKENS

NEG = -1.0e9          # 가릴 칸의 로짓. 유한한 값이라 전부 가려진 행에서도 softmax가 NaN을 내지 않는다
FAMILIES = ("nrms", "sasrec")


@dataclass(frozen=True)
class NeuralSpec:
    family: str
    d: int
    heads: int
    dropout: float
    lr: float
    weight_decay: float
    batch: int
    n_hist: int
    layers: int = 1
    aux_lambda: float = 0.0
    use_scalars: bool = True

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ValueError(f"알 수 없는 family: {self.family}")
        if self.d % self.heads:
            raise ValueError("d는 heads로 나누어떨어져야 합니다")

    @classmethod
    def from_config(cls, family: str, config: Mapping, use_scalars: bool = True) -> "NeuralSpec":
        return cls(family=family, d=int(config["d"]), heads=int(config["heads"]), dropout=float(config["dropout"]),
                   lr=float(config["lr"]), weight_decay=float(config["weight_decay"]), batch=int(config["batch"]),
                   n_hist=int(config["n_hist"]), layers=int(config.get("layers", 1)),
                   aux_lambda=float(config.get("aux_lambda", 0.0)), use_scalars=use_scalars)

    def to_dict(self) -> dict:
        return asdict(self)


class OneHotEmbedding(nn.Module):
    """작은 범주(카테고리, 성별, 위치)의 임베딩을 one-hot 행렬곱으로 계산한다. 역전파에 인덱스 누적 연산이 없어
    결정론 모드의 GPU에서도 같은 값이 나온다."""

    def __init__(self, n: int, dim: int):
        super().__init__()
        self.n = n
        self.weight = nn.Parameter(torch.randn(n, dim) * 0.02)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        return F.one_hot(idx, self.n).to(self.weight.dtype) @ self.weight


class ItemProjection(nn.Module):
    """고정 기사 벡터 -> d차원. 학습되는 유일한 아이템 쪽 파라미터다."""

    def __init__(self, emb_dim: int, d: int, dropout: float):
        super().__init__()
        self.lin, self.norm, self.drop = nn.Linear(emb_dim, d), nn.LayerNorm(d), nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.drop(self.norm(self.lin(x)))


class SelfAttention(nn.Module):
    """multi-head self-attention. allow[b, i, j]가 참인 칸만 본다."""

    def __init__(self, d: int, heads: int, dropout: float):
        super().__init__()
        self.heads = heads
        self.qkv, self.out, self.drop = nn.Linear(d, 3 * d), nn.Linear(d, d), nn.Dropout(dropout)

    def forward(self, x: torch.Tensor, allow: torch.Tensor) -> torch.Tensor:
        b, n, d = x.shape
        q, k, v = self.qkv(x).view(b, n, 3, self.heads, d // self.heads).permute(2, 0, 3, 1, 4)
        att = (q @ k.transpose(-1, -2)) / math.sqrt(d // self.heads)
        w = self.drop(torch.softmax(att.masked_fill(~allow[:, None], NEG), dim=-1))
        return self.out((w @ v).transpose(1, 2).reshape(b, n, d))


class NRMSUserEncoder(nn.Module):
    """self-attention 뒤 additive attention으로 한 벡터로 모은다(NRMS의 유저 인코더)."""

    def __init__(self, d: int, heads: int, dropout: float):
        super().__init__()
        self.attn, self.drop = SelfAttention(d, heads, dropout), nn.Dropout(dropout)
        self.add_proj, self.add_query = nn.Linear(d, d), nn.Linear(d, 1, bias=False)

    def forward(self, x: torch.Tensor, mask: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        h = self.drop(self.attn(x, mask[:, None, :].expand(-1, x.shape[1], -1)))
        a = self.add_query(torch.tanh(self.add_proj(h))).squeeze(-1).masked_fill(~mask, NEG)
        w = torch.softmax(a, dim=-1) * mask
        return (w[..., None] * h).sum(dim=1), h


class _Block(nn.Module):
    def __init__(self, d: int, heads: int, dropout: float, ffn_mult: int):
        super().__init__()
        self.n1, self.n2 = nn.LayerNorm(d), nn.LayerNorm(d)
        self.attn, self.drop = SelfAttention(d, heads, dropout), nn.Dropout(dropout)
        self.ffn = nn.Sequential(nn.Linear(d, ffn_mult * d), nn.GELU(), nn.Dropout(dropout), nn.Linear(ffn_mult * d, d))

    def forward(self, h: torch.Tensor, allow: torch.Tensor) -> torch.Tensor:
        h = h + self.drop(self.attn(self.n1(h), allow))
        return h + self.drop(self.ffn(self.n2(h)))


class SASRecUserEncoder(nn.Module):
    """인과 self-attention(pre-LN). 위치 임베딩은 최근성 순번(가장 최근 클릭 = 0)이라 패딩 길이와 무관하다."""

    def __init__(self, d: int, heads: int, layers: int, n_positions: int, dropout: float, ffn_mult: int):
        super().__init__()
        self.pos = OneHotEmbedding(n_positions, d)
        self.blocks = nn.ModuleList([_Block(d, heads, dropout, ffn_mult) for _ in range(layers)])
        self.norm, self.drop = nn.LayerNorm(d), nn.Dropout(dropout)

    def forward(self, x: torch.Tensor, mask: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        n = x.shape[1]
        recency = torch.arange(n - 1, -1, -1, device=x.device)
        h = self.drop(x + self.pos(recency)[None])
        allow = torch.tril(torch.ones(n, n, dtype=torch.bool, device=x.device))[None] & mask[:, None, :]
        for block in self.blocks:
            h = block(h, allow)
        h = self.norm(h) * mask[..., None]
        return h[:, -1], h          # 오른쪽 정렬이라 마지막 칸이 가장 최근 클릭이다


class LateFusionHead(nn.Module):
    """s = MLP2([x̃, <u,c>/sqrt(d), W_p(u*c)]). use_scalars가 거짓이면 x̃를 뺀다(B0)."""

    def __init__(self, x_dim: int, d: int, hidden: int, interaction_dim: int, dropout: float, use_scalars: bool):
        super().__init__()
        self.d, self.use_scalars = d, use_scalars
        self.inter = nn.Linear(d, interaction_dim)
        width = (x_dim if use_scalars else 0) + 1 + interaction_dim
        self.mlp = nn.Sequential(nn.Linear(width, hidden), nn.ReLU(), nn.Dropout(dropout), nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor, u: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
        uc = c * u[:, None, :]
        parts = [uc.sum(dim=-1, keepdim=True) / math.sqrt(self.d), self.inter(uc)]
        return self.mlp(torch.cat(([x] if self.use_scalars else []) + parts, dim=-1)).squeeze(-1)


class NeuralRanker(nn.Module):
    def __init__(self, spec: NeuralSpec, emb_dim: int, n_cont: int, n_categories: int, fixed: Mapping):
        super().__init__()
        self.spec = spec
        self.proj = ItemProjection(emb_dim, spec.d, spec.dropout)
        self.cat_emb = OneHotEmbedding(max(n_categories, 1), int(fixed["category_emb_dim"]))
        self.gender_emb = OneHotEmbedding(GENDER_TOKENS, int(fixed["gender_emb_dim"]))
        if spec.family == "nrms":
            self.encoder: nn.Module = NRMSUserEncoder(spec.d, spec.heads, spec.dropout)
        else:
            self.encoder = SASRecUserEncoder(spec.d, spec.heads, spec.layers, spec.n_hist, spec.dropout,
                                             int(fixed["ffn_mult"]))
        x_dim = n_cont + int(fixed["category_emb_dim"]) + int(fixed["gender_emb_dim"])
        self.head = LateFusionHead(x_dim, spec.d, int(fixed["fusion_hidden"]), int(fixed["interaction_dim"]),
                                   spec.dropout, spec.use_scalars)

    def encode_user(self, emb: torch.Tensor, seq_items: torch.Tensor, seq_mask: torch.Tensor):
        """(u, 위치별 출력 h, 투영된 시퀀스). emb의 마지막 행은 패딩용 0 벡터다. 빈 시퀀스의 u는 정확히 0이다."""
        pad = emb.shape[0] - 1
        x = self.proj(emb[torch.where(seq_mask, seq_items, pad)]) * seq_mask[..., None]
        u, h = self.encoder(x, seq_mask)
        return u * seq_mask.any(dim=1, keepdim=True), h, x

    def forward(self, emb: torch.Tensor, batch: Mapping[str, torch.Tensor]):
        u, h, seq_proj = self.encode_user(emb, batch["seq_items"], batch["seq_mask"])
        pad = emb.shape[0] - 1
        c = self.proj(emb[torch.where(batch["cand_mask"], batch["cand_item"], pad)])
        x = torch.cat([batch["cont"], self.cat_emb(batch["category"]), self.gender_emb(batch["gender"])], dim=-1)
        return self.head(x, u, c), h, seq_proj


def listwise_loss(scores: torch.Tensor, labels: torch.Tensor, cand_mask: torch.Tensor) -> torch.Tensor:
    """노출 단위 masked softmax 교차 엔트로피. 양성이 여럿이면 양성마다의 −log p를 그 노출 안에서 평균한다.
    양성이 없는 노출은 뺀다."""
    logp = torch.log_softmax(scores.masked_fill(~cand_mask, NEG), dim=-1)
    n_pos = labels.sum(dim=-1)
    per_group = -(logp * labels).sum(dim=-1) / n_pos.clamp(min=1.0)
    has = (n_pos > 0).to(per_group.dtype)
    return (per_group * has).sum() / has.sum().clamp(min=1.0)


def next_click_loss(model: NeuralRanker, emb: torch.Tensor, h: torch.Tensor, seq_proj: torch.Tensor,
                    seq_mask: torch.Tensor, aux_neg: Optional[torch.Tensor]) -> torch.Tensor:
    """보조 손실: 위치 i의 출력으로 i+1번째 클릭을, 미리 뽑아 둔 네거티브들과 대비한다. 유효한 위치가 없으면 0."""
    valid = seq_mask[:, :-1] & seq_mask[:, 1:]
    if aux_neg is None or not bool(valid.any()):
        return h.sum() * 0.0
    q = h[:, :-1]
    pos_logit = (q * seq_proj[:, 1:]).sum(dim=-1, keepdim=True)
    neg_ids = aux_neg[:, 1:]
    neg_ok = neg_ids >= 0
    neg = model.proj(emb[torch.where(neg_ok, neg_ids, emb.shape[0] - 1)])
    neg_logit = (q[:, :, None, :] * neg).sum(dim=-1).masked_fill(~neg_ok, NEG)
    logp = torch.log_softmax(torch.cat([pos_logit, neg_logit], dim=-1) / math.sqrt(model.spec.d), dim=-1)[..., 0]
    return -(logp * valid).sum() / valid.sum()
