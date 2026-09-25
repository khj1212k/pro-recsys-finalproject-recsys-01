import os
import sys
import types

# ai_workspace/recommend_engine을 sys.path에 추가 (src.* 임포트를 위해)
RECOMMEND_ENGINE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ai_workspace", "recommend_engine",
)
if RECOMMEND_ENGINE_ROOT not in sys.path:
    sys.path.insert(0, RECOMMEND_ENGINE_ROOT)

# src.utils.__init__이 BGE-M3 임베더(torch 필요)를 무조건 re-export하는데,
# 이 테스트들은 실제 임베딩 모델을 쓰지 않으므로 무거운 torch 설치 없이
# import만 통과하도록 최소 스텁을 주입한다.
# Tensor 속성이 없으면 scipy(array-api-compat)가 is_torch_array() 체크 중
# `torch.Tensor`에 접근하다 AttributeError로 죽으므로 최소한의 더미 클래스를 채워둔다.
if "torch" not in sys.modules:
    torch_stub = types.ModuleType("torch")
    torch_stub.Tensor = type("Tensor", (), {})
    sys.modules["torch"] = torch_stub
