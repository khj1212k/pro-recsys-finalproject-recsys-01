import torch
from FlagEmbedding import BGEM3FlagModel

class BGEEmbeddingModel:
    def __init__(self, model_config):
        model_name = model_config.get('name', 'BAAI/bge-m3')
        use_fp16 = model_config.get('use_fp16', True)
        
        # 디바이스 자동 감지 (config에 있어도 실제 하드웨어 우선 확인)
        device = model_config.get('device', 'cpu')
        if device == 'cuda' and not torch.cuda.is_available():
            print("Warning: CUDA requested but not available. Switching to CPU.")
            device = 'cpu'
            
        print(f">> [Model] Loading {model_name} on {device}...")
        self.device = device
        self.model = BGEM3FlagModel(
            model_name, 
            use_fp16=use_fp16, 
            device=device
        )

    def encode(self, text_list):
        """텍스트 리스트 -> Dense Vector (Tensor)"""
        if not text_list:
            return None
        
        with torch.no_grad():
            output = self.model.encode(
                text_list, 
                return_dense=True, 
                return_sparse=False, 
                return_colbert_vecs=False
            )
            return torch.from_numpy(output['dense_vecs']).to(self.device)
