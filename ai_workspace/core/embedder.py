# Stage3: 기사 임베딩 생성
# - BGE-M3 모델로 기사 제목+본문을 1024차원 벡터로 변환
# - GPU 사용 가능 시 자동 감지

import time
import logging
import os
import gc
from typing import Optional, List, Tuple, Any

import numpy as np

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

logger = logging.getLogger(__name__)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)



def plan_batches(token_lengths: List[int], max_batch: int, max_length: int, attention_budget: int) -> List[List[int]]:
    """입력 인덱스를 토큰 길이순으로 정렬해 배치로 묶는다.

    FlagEmbedding 1.2.5의 BGEM3FlagModel.encode는 배치를 가장 긴 입력 길이로 패딩하고,
    transformers 4.38.2의 XLM-R은 eager attention이라 배치마다 (배치 x 헤드 x L x L) 점수 텐서를
    만든다. 그래서 배치 크기 x min(최대 길이, max_length)^2 <= attention_budget으로 묶는다
    (예산을 넘는 입력 하나는 혼자 배치 - 항상 진행한다).
    """
    order = sorted(range(len(token_lengths)), key=lambda i: token_lengths[i])
    groups: List[List[int]] = []
    current: List[int] = []
    for i in order:
        # 오름차순이므로 새 원소가 곧 배치의 최대 길이다
        longest = min(token_lengths[i], max_length)
        if current and (len(current) >= max_batch or (len(current) + 1) * longest ** 2 > attention_budget):
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    return groups


class NewsEmbedder:
    
    # BGE-M3 기반 텍스트 임베딩 모델 (1024차원)
    

    def __init__(self, force_cpu: bool = False, verbose: bool = True, l2_normalize: bool = True,
                 max_length: Optional[int] = None, attention_budget: Optional[int] = None):
        from config.settings import Settings

        self.verbose = verbose
        self.l2_normalize = l2_normalize
        self.max_length = max_length or Settings.EMBEDDING_MAX_LENGTH
        self.attention_budget = attention_budget or Settings.EMBEDDING_ATTENTION_BUDGET
        self.device = self._get_device(force_cpu)
        self.model: Any = None
        
        if self.verbose:
            logger.info(f"🔌 BGE-M3 모델 로딩 중... (장치: {self.device})")
        
        from FlagEmbedding import BGEM3FlagModel
        
        start_time = time.time()
        self.model = BGEM3FlagModel(
            'BAAI/bge-m3',
            use_fp16=(self.device == 'cuda'),
            device=self.device
        )
        load_time = time.time() - start_time
        
        if self.verbose:
            logger.info(f"✅ 모델 로드 완료! ({load_time:.2f}s) | L2 정규화: {'ON' if l2_normalize else 'OFF'}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def _get_device(self, force_cpu: bool) -> str:
        if force_cpu:
            return 'cpu'
        import torch

        
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            if self.verbose:
                logger.info(f"🎮 GPU Detected: {device_name}")
            return 'cuda'
        elif torch.backends.mps.is_available():
            return 'mps'
        return 'cpu'

    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 20) -> Tuple[List[Any], float]:
        """texts를 입력 순서 그대로의 임베딩 리스트로 돌려준다. batch_size는 한 번에 인코딩할 최대 건수이고,
        실제 배치는 plan_batches가 토큰 길이와 attention 예산으로 정한다."""
        if not texts or not self.model:
            return [], 0.0

        start_time = time.time()
        lengths = [len(ids) for ids in self.model.tokenizer(
            list(texts), truncation=True, max_length=self.max_length)["input_ids"]]
        dense: List[Any] = [None] * len(texts)
        for group in plan_batches(lengths, batch_size, self.max_length, self.attention_budget):
            try:
                output = self.model.encode(
                    [texts[i] for i in group],
                    batch_size=len(group),
                    max_length=self.max_length,
                    return_dense=True,
                    return_sparse=False,
                    return_colbert_vecs=False
                )
            except Exception as e:
                logger.error(f"❌ Batch embedding failed: {e}")
                raise
            for i, vec in zip(group, output['dense_vecs']):
                dense[i] = vec
            # MPS 할당자는 길이가 다른 큰 텐서 블록을 재사용하지 못하고 캐시에 쌓는다 - 4,000토큰대
            # 기사 8건을 한 건씩 처리하는 동안 드라이버 메모리가 3GB에서 14GB까지 커졌다(ADR 0006).
            self._release_device_cache()

        embeddings = np.asarray(dense, dtype=np.float32)
        if self.l2_normalize:
            # torch.nn.functional.normalize(p=2, dim=1)과 같은 계산(eps=1e-12)
            embeddings = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-12)

        elapsed = time.time() - start_time
        return embeddings.tolist(), elapsed

    def _release_device_cache(self) -> None:
        if self.device == 'mps':
            import torch
            torch.mps.empty_cache()
        elif self.device == 'cuda':
            import torch
            torch.cuda.empty_cache()

    def cleanup(self):
        if self.model:
            del self.model
            self.model = None
        
        if self.device == 'cuda':
            import torch
            torch.cuda.empty_cache()
        elif self.device == 'mps':
            import torch
            torch.mps.empty_cache()

        gc.collect()
        if self.verbose:
            logger.info("🧹 GPU Memory Cleaned")
