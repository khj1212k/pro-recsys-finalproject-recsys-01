"""
Core Embedder Module
Generates 1024-dimensional embeddings using BGE-M3 model.
Supports GPU acceleration and batch processing.
"""
import torch
import time
import logging
import os
import gc
from typing import Optional, List, Tuple, Any

# Suppress warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

# Standard logger
# from utils.logger import setup_logger
# logger = setup_logger(__name__, logging.INFO)
logger = logging.getLogger(__name__)

# Suppress external logs
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

# from FlagEmbedding import BGEM3FlagModel
# Lazy import in __init__



class NewsEmbedder:
    """
    BGE-M3 기반 텍스트 임베딩 모델 (1024차원)
    
    GPU 가속을 지원하며, 컨텍스트 매니저(with 문) 패턴을 통해 
    자동으로 리소스(GPU 메모리)를 정리합니다.
    """

    def __init__(self, force_cpu: bool = False, verbose: bool = True, l2_normalize: bool = True):
        self.verbose = verbose
        self.l2_normalize = l2_normalize
        self.device = self._get_device(force_cpu)
        self.model: Any = None
        
        if self.verbose:
            logger.info(f"🔌 BGE-M3 모델 로딩 중... (장치: {self.device})")
        
        # Lazy import (모듈 임포트 지연)
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
        """컨텍스트 매니저 진입"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """컨텍스트 매니저 종료 - 자동 리소스 정리"""
        self.cleanup()
        return False

    def _get_device(self, force_cpu: bool) -> str:
        if force_cpu:
            return 'cpu'
        
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            if self.verbose:
                logger.info(f"🎮 GPU Detected: {device_name}")
            return 'cuda'
        elif torch.backends.mps.is_available():
            return 'mps'
        return 'cpu'

    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 20) -> Tuple[List[Any], float]:
        """
        Generate embeddings for a list of texts in batches.
        
        Args:
            texts: List of strings to embed
            batch_size: Batch size for processing
            
        Returns:
            Tuple of (embeddings list, processing time)
        """
        if not texts or not self.model:
            return [], 0.0

        all_embeddings = []
        start_time = time.time()
        total_texts = len(texts)
        
        for i in range(0, total_texts, batch_size):
            batch_texts = texts[i : i + batch_size]
            try:
                # BGE-M3 모델은 'dense', 'sparse', 'colbert' 3가지 임베딩을 딕셔너리로 반환함
                # 여기서 우리는 'dense_vecs'만 필요하므로 추출해서 사용
                # (FlagEmbedding 래퍼 내부 로직에 따라 반환 타입이 다를 수 있어 확인 필요)
                
                # We use the standard API for BGEM3FlagModel
                output = self.model.encode(
                    batch_texts, 
                    batch_size=batch_size, 
                    max_length=8192,
                    return_dense=True, 
                    return_sparse=False, 
                    return_colbert_vecs=False
                )
                
                # output['dense_vecs'] is the array if return dictionary
                embeddings = output['dense_vecs']
                
                if self.l2_normalize:
                    embeddings = torch.tensor(embeddings)
                    embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1) # p: 2-norm, dim: 1차원
                    embeddings = embeddings.cpu().numpy().tolist() # DB에 저장하기위해 list로 변환
                else:
                    embeddings = embeddings.tolist()
                
                all_embeddings.extend(embeddings)
                
            except Exception as e:
                logger.error(f"❌ Batch embedding failed: {e}")
                raise e

        # Explicit GPU Cache Cleanup
        if self.device == 'cuda':
            torch.cuda.empty_cache()

        elapsed = time.time() - start_time
        return all_embeddings, elapsed

    def cleanup(self):
        """Release GPU resources"""
        if self.model:
            del self.model
            self.model = None
        
        if self.device == 'cuda':
            torch.cuda.empty_cache()
            
        gc.collect()
        if self.verbose:
            logger.info("🧹 GPU Memory Cleaned")
