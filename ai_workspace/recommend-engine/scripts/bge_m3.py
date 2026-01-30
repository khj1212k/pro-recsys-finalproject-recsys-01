import torch
from FlagEmbedding import BGEM3FlagModel


class BGEEmbeddingModel:
    """BGE-M3 모델을 사용한 텍스트 임베딩 클래스"""

    def __init__(self, model_config):
        """
        BGE-M3 임베딩 모델 초기화.
        1. 설정에서 모델명, FP16, device 추출
        2. CUDA 사용 가능 여부 확인
        3. 모델 로드
        """
        model_name = model_config.get('name', 'BAAI/bge-m3') # 사용할 모델 이름 (기본값: BAAI/bge-m3)
        use_fp16 = model_config.get('use_fp16', True) # FP16 사용 여부 (메모리 절약용)
        
        device = model_config.get('device', 'cpu')
        if device == 'cuda' and not torch.cuda.is_available():
            print("Warning: CUDA requested but not available. Switching to CPU.")
            device = 'cpu'
            
        print(f">> [Model] Loading {model_name} on {device}...")
        self.device = device # 현재 사용 중인 device
        self.model = BGEM3FlagModel( # BGE-M3 모델 인스턴스
            model_name, 
            use_fp16=use_fp16, 
            device=device
        )

    def encode(self, text_list):
        """
        텍스트 리스트를 Dense Vector로 변환.
        
        반환: 임베딩 Tensor (shape: (N, Dim)) 또는 빈 리스트일 경우 None
        """
        if not text_list:
            return None
        
        with torch.no_grad():
            output = self.model.encode( # 모델 추론 결과 (dense_vecs 포함)
                text_list, 
                return_dense=True, 
                return_sparse=False, 
                return_colbert_vecs=False
            )
            return torch.from_numpy(output['dense_vecs']).to(self.device) # numpy 배열을 Tensor로 변환
