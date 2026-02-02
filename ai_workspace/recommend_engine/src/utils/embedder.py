# src/utils/embedder.py
"""BGE-M3 임베딩 생성 모듈 (1024차원 고정)"""

import torch
import math
import time
import logging
import os
from typing import List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
logging.getLogger("transformers").setLevel(logging.ERROR)

try:
    from FlagEmbedding import BGEM3FlagModel
    HAS_FLAG_EMBEDDING = True
except ImportError:
    HAS_FLAG_EMBEDDING = False


class BGEEmbedder:
    """BGE-M3 기반 임베딩 생성기 (1024d 고정)"""
    
    EMBEDDING_DIM = 1024
    
    def __init__(self, force_cpu: bool = False, verbose: bool = True):
        if not HAS_FLAG_EMBEDDING:
            raise ImportError("FlagEmbedding not installed")
        
        self.verbose = verbose
        self.device = "cpu" if force_cpu else ("cuda" if torch.cuda.is_available() else "cpu")
        
        if self.verbose:
            print(f"🔌 BGE-M3 모델 로딩 중... (Device: {self.device})")
        
        load_start = time.time()
        self.model = BGEM3FlagModel(
            "BAAI/bge-m3",
            use_fp16=(self.device == "cuda"),
            device=self.device
        )
        
        if self.verbose:
            print(f"✅ 모델 로딩 완료! ({time.time() - load_start:.2f}초)")
    
    def _l2_normalize(self, vec: List[float]) -> List[float]:
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec
    
    def encode_single(self, text: str) -> Optional[List[float]]:
        if not text or not text.strip():
            return None
        results, _ = self.encode_batch([text])
        return results[0] if results else None
    
    def encode_batch(
        self, 
        texts: List[str], 
        batch_size: int = 16
    ) -> Tuple[List[Optional[List[float]]], float]:
        if not texts:
            return [], 0.0
        
        start_time = time.time()
        
        valid_indices, valid_texts = [], []
        for i, text in enumerate(texts):
            if text and text.strip():
                valid_indices.append(i)
                valid_texts.append(text)
        
        if not valid_texts:
            return [None] * len(texts), 0.0
        
        try:
            output = self.model.encode(valid_texts, batch_size=batch_size, max_length=8192)
            embeddings = [self._l2_normalize(emb.tolist()) for emb in output["dense_vecs"]]
            
            results = [None] * len(texts)
            for idx, emb in zip(valid_indices, embeddings):
                results[idx] = emb
            
            return results, time.time() - start_time
        except Exception as e:
            if self.verbose:
                print(f"⚠️ 배치 임베딩 실패: {e}")
            return [None] * len(texts), 0.0
    
    def encode_news(self, title: str, content: str, category: str = "") -> Optional[List[float]]:
        parts = [f"제목: {title}"]
        if category:
            parts.append(f"카테고리: {category}")
        if content:
            parts.append(f"내용: {content}")
        return self.encode_single("\n".join(parts))


_embedder_instance = None

def get_embedder(force_cpu: bool = False, verbose: bool = True) -> BGEEmbedder:
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = BGEEmbedder(force_cpu=force_cpu, verbose=verbose)
    return _embedder_instance

def embed_text(text: str) -> Optional[List[float]]:
    return get_embedder(verbose=False).encode_single(text)

def embed_texts(texts: List[str], batch_size: int = 16) -> List[Optional[List[float]]]:
    results, _ = get_embedder(verbose=False).encode_batch(texts, batch_size)
    return results
