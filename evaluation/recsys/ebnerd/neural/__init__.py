"""EB-NeRD 신경망 사용자 모델 비교(E15)용 패키지 자리.

지금 들어 있는 것은 콜드 조건 정의(`cold.py`)뿐이다. 이 모듈은 numpy/pandas만 쓰므로 torch 없이 임포트되고,
콜드 regime 사슬(`run_cold.py`)과 E15가 같은 정의를 쓰도록 여기 한 곳에 둔다(ADR 0013 A2 사전 등록 A2.3).
"""
