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
from utils.logger import setup_logger
logger = setup_logger(__name__, logging.INFO)

# Suppress external logs
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

from FlagEmbedding import BGEM3FlagModel


class NewsEmbedder:
    """
    BGE-M3 based text embedder (1024 dimensions)
    Supports Context Manager pattern for automatic resource cleanup.
    """

    def __init__(self, force_cpu: bool = False, verbose: bool = True, l2_normalize: bool = True):
        self.verbose = verbose
        self.l2_normalize = l2_normalize
        self.device = self._get_device(force_cpu)
        self.model: Optional[BGEM3FlagModel] = None
        
        if self.verbose:
            logger.info(f"🔌 BGE-M3 model loading... (Device: {self.device})")
        
        start_time = time.time()
        self.model = BGEM3FlagModel(
            'BAAI/bge-m3',
            use_fp16=(self.device == 'cuda'),
            device=self.device
        )
        load_time = time.time() - start_time
        
        if self.verbose:
            logger.info(f"✅ Model loaded! ({load_time:.2f}s) | L2 Norm: {'ON' if l2_normalize else 'OFF'}")

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatic cleanup"""
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
                # BGE-M3 encode returns a dict with 'dense_vecs', 'colbert_vecs', 'sparse_vecs'
                # But FlagEmbedding wrapper encode usually returns dense vectors directly 
                # OR dict if return_dense=True etc.
                # Let's check original usage. It seemed to call self.model.encode(..., return_dense=True)
                
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
                    embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
                    embeddings = embeddings.cpu().numpy().tolist()
                else:
                    if hasattr(embeddings, 'tolist'):
                        embeddings = embeddings.tolist()
                
                all_embeddings.extend(embeddings)
                
            except Exception as e:
                logger.error(f"❌ Batch embedding failed: {e}")
                # Append None or zeros? For now, re-raise to handle upstream
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
