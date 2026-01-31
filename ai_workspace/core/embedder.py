"""
analyzer/embedder.py
: BGE-M3 모델을 이용한 1024차원 임베딩 생성기 (Quiet Mode 지원)
+ (추가) L2 정규화 옵션 지원 (기본: True)
"""

import torch
import time
import logging
import os
import math
from typing import Optional, List, Tuple, Any

# 모든 로그 / tqdm 제거
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

# huggingface transformers warning/log 제거
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)

# tqdm 제거
try:
    import tqdm
    tqdm.tqdm = lambda *args, **kwargs: args[0]  # dummy
except Exception:
    pass

from FlagEmbedding import BGEM3FlagModel


class NewsEmbedder:
    """
    BGE-M3 기반 텍스트 임베딩 (1024차원)
    GPU 가용 시 자동 CUDA, 아니면 CPU
    - (추가) L2 정규화 옵션: l2_normalize=True 이면 반환 벡터를 L2 normalize
    """

    def __init__(self, force_cpu: bool = False, verbose: bool = True, l2_normalize: bool = True):
        self.verbose = verbose
        self.l2_normalize = l2_normalize
        self.device = self._get_device(force_cpu)
        self.model: Any = None
        self.load_time = 0.0

        if self.verbose:
            print(f"🔌 BGE-M3 모델 로딩 중... (Device: {self.device})")

        load_start = time.time()

        # GPU -> fp16, CPU -> fp32
        use_fp16 = (self.device == "cuda")
        self.model = BGEM3FlagModel(
            "BAAI/bge-m3",
            use_fp16=use_fp16,
            device=self.device
        )

        self.load_time = time.time() - load_start

        if self.verbose:
            norm_msg = "ON" if self.l2_normalize else "OFF"
            print(f"✅ 모델 로딩 완료! (소요시간: {self.load_time:.2f}초) | L2 Normalize: {norm_msg}")

    def _get_device(self, force_cpu: bool) -> str:
        if force_cpu:
            return "cpu"

        if torch.cuda.is_available():
            if self.verbose:
                gpu_name = torch.cuda.get_device_name(0)
                try:
                    gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                    print(f"🎮 GPU 감지됨: {gpu_name} ({gpu_memory:.1f}GB)")
                except:
                    print(f"🎮 GPU 감지됨: {gpu_name}")
            return "cuda"
        else:
            if self.verbose:
                print("💻 GPU 미감지 - CPU 모드로 실행")
            return "cpu"

    def _l2_normalize_vec(self, vec: List[float]) -> List[float]:
        """L2 정규화 (0벡터면 그대로 반환)"""
        s = 0.0
        for x in vec:
            fx = float(x)
            s += fx * fx
        norm = math.sqrt(s)
        if norm <= 0.0:
            return vec
        inv = 1.0 / norm
        return [float(x) * inv for x in vec]

    def generate_embedding(self, text: str) -> Optional[List[float]]:
        if not text or not text.strip():
            return None

        try:
            output = self.model.encode(
                [text],
                batch_size=1,
                max_length=8192
            )
            vec = output["dense_vecs"][0].tolist()
            if self.l2_normalize and vec:
                vec = self._l2_normalize_vec(vec)
            return vec
        except Exception as e:
            if self.verbose:
                print(f"⚠️ 임베딩 생성 실패: {e}")
            return None

    def generate_embeddings_batch(
        self,
        texts: List[str],
        batch_size: int = 8
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
            output = self.model.encode(
                valid_texts,
                batch_size=batch_size,
                max_length=8192
            )

            dense = output["dense_vecs"].tolist()
            if self.l2_normalize:
                dense = [self._l2_normalize_vec(v) for v in dense]

            results = [None] * len(texts)
            for idx, emb in zip(valid_indices, dense):
                results[idx] = emb

        except Exception as e:
            if self.verbose:
                print(f"⚠️ 배치 임베딩 실패: {e}")
            results = [None] * len(texts)

        return results, time.time() - start_time

    def cleanup(self):
        """명시적으로 모델을 메모리에서 해제하고 GPU 캐시를 비웁니다."""
        if self.model is not None:
            del self.model
            self.model = None
        
        if self.device == "cuda":
            import torch
            torch.cuda.empty_cache()
            if self.verbose:
                print("🧹 GPU 메모리 정리 완료")

    def get_device_info(self) -> dict:
        info = {
            "device": self.device,
            "model_load_time": self.load_time,
            "l2_normalize": self.l2_normalize,
        }

        if self.device == "cuda":
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["gpu_memory_total"] = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            info["gpu_memory_allocated"] = torch.cuda.memory_allocated(0) / (1024**3)
        else:
            info["cpu_count"] = os.cpu_count()

        return info


# ============================================================
# 벤치마크 
# ============================================================
def benchmark_embedding(sample_texts=None, batch_size=8):
    if sample_texts is None:
        sample_texts = [
            "대통령이 국회에서 연설했다.", "삼성전자 주가 상승", "강남 대형 화재",
            "미중 무역 협상", "AI 기술 발전"
        ] * 4

    print("📊 BGE-M3 임베딩 벤치마크")

    print("\n[1] GPU 테스트")
    try:
        embedder_gpu = NewsEmbedder(force_cpu=False, l2_normalize=True)
        _, gpu_time = embedder_gpu.generate_embeddings_batch(sample_texts, batch_size)
        print(f"GPU 처리: {len(sample_texts)}건 / {gpu_time:.2f}초")
    except Exception as e:
        print(f"GPU 실패: {e}")
        gpu_time = None

    print("\n[2] CPU 테스트")
    embedder_cpu = NewsEmbedder(force_cpu=True, l2_normalize=True)
    _, cpu_time = embedder_cpu.generate_embeddings_batch(sample_texts, batch_size=4)
    print(f"CPU 처리: {len(sample_texts)}건 / {cpu_time:.2f}초")

    print("\n📈 요약")
    if gpu_time:
        print(f"GPU Speedup: {cpu_time / gpu_time:.1f}x")
    print("완료")


if __name__ == "__main__":
    benchmark_embedding()
