# Stage3: 기사 임베딩 생성
# - BGE-M3 모델로 기사 제목+본문을 1024차원 벡터로 변환
# - GPU 사용 가능 시 자동 감지

import torch
import time
import logging
import os
import gc
from typing import Optional, List, Tuple, Any

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

logger = logging.getLogger(__name__)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)



class NewsEmbedder:
    
    # BGE-M3 기반 텍스트 임베딩 모델 (1024차원)
    

    def __init__(
        self,
        force_cpu: bool = False,
        verbose: bool = True,
        l2_normalize: bool = True,
        max_length: Optional[int] = None,
        use_fp16: Optional[bool] = None,
    ):
        # max_length: 토큰 단위 절단 길이. None이면 Settings.EMBEDDING_MAX_LENGTH(없으면 기존 파이프라인 값
        # 8192). 대량 오프라인 임베딩(EB-NeRD 벤치마크)은 attention 비용 때문에 512로 줄여 쓴다.
        # use_fp16=None이면 기존 동작(CUDA에서만 fp16). MPS에서 True로 주면 처리량이 늘고 dense 벡터는
        # fp32와 사실상 같다(공유 M2에서 기사 96건, 512 토큰: 1.48 -> 2.18건/s, fp16-fp32 코사인 1.0000).
        from config.settings import Settings

        self.verbose = verbose
        self.l2_normalize = l2_normalize
        self.max_length = max_length or getattr(Settings, "EMBEDDING_MAX_LENGTH", 8192)
        self.device = self._get_device(force_cpu)
        self.model: Any = None
        
        if self.verbose:
            logger.info(f"🔌 BGE-M3 모델 로딩 중... (장치: {self.device})")
        
        from FlagEmbedding import BGEM3FlagModel
        
        start_time = time.time()
        self.model = BGEM3FlagModel(
            'BAAI/bge-m3',
            use_fp16=(self.device == 'cuda') if use_fp16 is None else use_fp16,
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
        
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            if self.verbose:
                logger.info(f"🎮 GPU Detected: {device_name}")
            return 'cuda'
        elif torch.backends.mps.is_available():
            return 'mps'
        return 'cpu'

    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 20) -> Tuple[List[Any], float]:
        if not texts or not self.model:
            return [], 0.0

        all_embeddings = []
        start_time = time.time()
        total_texts = len(texts)
        
        for i in range(0, total_texts, batch_size):
            batch_texts = texts[i : i + batch_size]
            try:
                # 'dense_vecs'만 추출
                output = self.model.encode(
                    batch_texts, 
                    batch_size=batch_size, 
                    max_length=self.max_length,
                    return_dense=True, 
                    return_sparse=False, 
                    return_colbert_vecs=False
                )
                
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

        if self.device == 'cuda':
            torch.cuda.empty_cache()

        elapsed = time.time() - start_time
        return all_embeddings, elapsed

    def cleanup(self):
        if self.model:
            del self.model
            self.model = None
        
        if self.device == 'cuda':
            torch.cuda.empty_cache()
            
        gc.collect()
        if self.verbose:
            logger.info("🧹 GPU Memory Cleaned")
