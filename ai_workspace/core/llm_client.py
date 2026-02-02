"""
다중 제공자를 위한 통합 LLM 클라이언트
지원: OpenAI, Naver HyperCLOVA X
"""
import os
import json
import time
import logging
import requests
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 10  # Maximum retry attempts
INITIAL_BACKOFF = 1.0  # Initial wait time in seconds
MAX_BACKOFF = 60.0  # Maximum wait time between retries
BACKOFF_MULTIPLIER = 2.0  # Exponential backoff multiplier


class BaseLLMClient(ABC):
    """LLM 클라이언트를 위한 추상 기본 클래스"""

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        """채팅 응답 생성"""
        pass


# Models that use v3 API
V3_MODELS = {"HCX-005", "HCX-007", "HCX-DASH-002"}
# Models that use v1 API
V1_MODELS = {"HCX-003", "HCX-DASH-001", "HCX-002"}


class NaverHyperCLOVAClient(BaseLLMClient):
    """
    네이버 하이퍼클로바 X (HyperCLOVA X) API 클라이언트

    지원 모델:
    - v1 API: HCX-003, HCX-DASH-001
    - v3 API: HCX-005, HCX-007, HCX-DASH-002

    인증 방식:
    - Apps 인증 (nv- 접두사): Bearer 토큰
    - Legacy 인증: X-NCP-CLOVASTUDIO-API-KEY + X-NCP-APIGW-API-KEY
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        apigw_key: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or os.getenv("NCP_CLOVASTUDIO_API_KEY")
        self.apigw_key = apigw_key or os.getenv("NCP_APIGW_API_KEY")
        self.model = model or os.getenv("HYPERCLOVA_MODEL", "HCX-003")

        if not self.api_key:
            raise ValueError("NCP_CLOVASTUDIO_API_KEY 환경변수를 설정하세요")

        # Detect authentication method based on key format
        self.use_apps_auth = self.api_key.startswith("nv-")

        if not self.use_apps_auth and not self.apigw_key:
            raise ValueError("NCP_APIGW_API_KEY 환경변수를 설정하세요 (Legacy 인증 방식)")

        # Determine API version based on model
        self.api_version = "v3" if self.model in V3_MODELS else "v1"

        # Set base URL
        self.base_url = f"https://clovastudio.stream.ntruss.com/testapp/{self.api_version}/chat-completions"

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Generate chat completion using HyperCLOVA X

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            response_format: Ignored for HyperCLOVA (JSON mode handled via prompt)

        Returns:
            Generated text content or None on error
        """
        # Set headers based on authentication method
        if self.use_apps_auth:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
        else:
            headers = {
                "X-NCP-CLOVASTUDIO-API-KEY": self.api_key,
                "X-NCP-APIGW-API-KEY": self.apigw_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

        # Build payload based on API version
        if self.api_version == "v3":
            # v3 API uses snake_case for max_tokens
            payload = {
                "messages": messages,
                "topP": 0.8,
                "max_tokens": max_tokens,  # snake_case for v3
                "temperature": temperature,
                "repeatPenalty": 1.2,
            }
            # HCX-007 supports thinking feature - disable by default for JSON responses
            if self.model == "HCX-007":
                payload["thinking"] = {"effort": "none"}
        else:
            # v1 API uses camelCase for maxTokens
            payload = {
                "messages": messages,
                "topP": 0.8,
                "topK": 0,
                "maxTokens": max_tokens,  # camelCase for v1
                "temperature": temperature,
                "repeatPenalty": 1.2,
                "stopBefore": [],
                "includeAiFilters": False,
                "seed": 0
            }

        url = f"{self.base_url}/{self.model}"

        # Infinite retry logic for rate limits and server errors
        # Will only give up on client errors (4xx except 429)
        backoff = INITIAL_BACKOFF
        attempt = 0

        while True:  # 무한 재시도
            attempt += 1
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=120
                )

                # Handle rate limit (429) with retry - 무한 재시도
                if response.status_code == 429:
                    retry_after = response.headers.get('Retry-After')
                    wait_time = float(retry_after) if retry_after else backoff
                    logger.debug("Rate limited (429). Waiting %.1fs before retry #%s", wait_time, attempt)
                    time.sleep(wait_time)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                response.raise_for_status()
                result = response.json()

                # HyperCLOVA X response format
                if result.get("status", {}).get("code") == "20000":
                    content = result.get("result", {}).get("message", {}).get("content", "")
                    return content
                else:
                    error_msg = result.get("status", {}).get("message", "Unknown error")
                    error_code = result.get("status", {}).get("code", "")

                    # Retry on server errors (5xx) - 무한 재시도
                    if error_code.startswith("5"):
                        logger.debug("Server error (%s): %s. Retrying #%s...", error_code, error_msg, attempt)
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue

                    # Client errors (4xx except 429) - give up
                    logger.debug("HyperCLOVA API client error (%s): %s", error_code, error_msg)
                    return None

            except requests.exceptions.Timeout:
                logger.debug("Request timeout. Retrying #%s...", attempt)
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except requests.exceptions.RequestException as e:
                # Check if it's a retryable error (connection error, 5xx, etc.)
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if status_code == 429 or status_code >= 500:
                        logger.debug("HTTP %s. Retrying #%s...", status_code, attempt)
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue
                    # Client errors - give up
                    logger.debug("HTTP %s error. Response body: %s", status_code, e.response.text[:500])
                    return None

                # Connection errors are retryable - 무한 재시도
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)):
                    logger.debug("Connection error. Retrying #%s...", attempt)
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.debug("HyperCLOVA API request failed: %s", e)
                return None

            except Exception as e:
                logger.debug("HyperCLOVA API unexpected error: %s", e)
                return None


class OpenAIClient(BaseLLMClient):
    """OpenAI API 클라이언트 (Fallback 또는 비교용)"""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai library required: pip install openai")

        self.model = model
        api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise ValueError("OPENAI_API_KEY 환경변수를 설정하세요")

        self.client = OpenAI(api_key=api_key)

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        """Generate chat completion using OpenAI with retry logic"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        if response_format:
            kwargs["response_format"] = response_format

        backoff = INITIAL_BACKOFF
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content

            except Exception as e:
                last_error = str(e)
                error_str = str(e).lower()

                # Retry on rate limit or server errors
                if "rate_limit" in error_str or "429" in error_str or "500" in error_str or "503" in error_str:
                    logger.debug("OpenAI API error. Retrying %s/%s...", attempt + 1, MAX_RETRIES)
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.debug("OpenAI API error: %s", e)
                return None

        logger.debug("Max retries (%s) exhausted. Last error: %s", MAX_RETRIES, last_error)
        return None


def get_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    """
    적절한 LLM 클라이언트를 반환하는 팩토리 함수

    Args:
        provider: 'naver', 'openai', 또는 None (환경변수에서 자동 감지)

    Returns:
        LLM 클라이언트 인스턴스
    """
    if provider is None:
        provider = os.getenv("LLM_PROVIDER", "naver").lower()

    if provider == "naver" or provider == "hyperclova":
        return NaverHyperCLOVAClient()
    elif provider == "openai":
        return OpenAIClient()
    else:
        # Default to Naver
        return NaverHyperCLOVAClient()


def extract_json_from_response(content: str) -> Optional[Dict]:
    """
    LLM 응답에서 JSON 추출 (복구 시도 포함)
    - 마크다운 코드블록 처리
    - 문자열 내부 개행/따옴표 복구 시도
    - 단일따옴표/파이썬 dict 형태 보정 시도
    """
    import re
    import ast

    if not content:
        return None

    def _clean(s: str) -> str:
        return s.strip().lstrip("\ufeff")

    def _repair_json_string(s: str) -> str:
        # trailing commas
        s = re.sub(r',\s*}', '}', s)
        s = re.sub(r',\s*]', ']', s)
        out = []
        in_str = False
        escape = False
        i = 0
        while i < len(s):
            ch = s[i]
            if in_str:
                if escape:
                    out.append(ch)
                    escape = False
                    i += 1
                    continue
                if ch == '\\\\':
                    out.append(ch)
                    escape = True
                    i += 1
                    continue
                if ch == '"':
                    j = i + 1
                    while j < len(s) and s[j] in ' \t\r\n':
                        j += 1
                    if j < len(s) and s[j] not in [',', '}', ']', ':']:
                        out.append('\\\"')
                    else:
                        out.append(ch)
                        in_str = False
                    i += 1
                    continue
                if ch == '\n':
                    out.append('\\n')
                    i += 1
                    continue
                if ch == '\r':
                    out.append('\\r')
                    i += 1
                    continue
                if ch == '\t':
                    out.append('\\t')
                    i += 1
                    continue
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
        return ''.join(out)

    def _find_balanced_json(text: str) -> Optional[str]:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == '\\\\':
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    candidates = []

    # 1) direct parse
    try:
        return json.loads(content)
    except Exception:
        candidates.append(content)

    # 2) markdown code blocks
    patterns = [
        r'```json\s*([\s\S]*?)\s*```',
        r'```\s*([\s\S]*?)\s*```',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            candidates.append(match.group(1))

    # 3) balanced brace block
    balanced = _find_balanced_json(content)
    if balanced:
        candidates.append(balanced)

    # 4) simple brace fallback
    brace_match = re.search(r'\{[\s\S]*\}', content)
    if brace_match:
        candidates.append(brace_match.group(0))

    for raw in candidates:
        if not raw:
            continue
        s = _clean(raw)
        if not s:
            continue
        # Try strict JSON
        try:
            return json.loads(s)
        except Exception:
            pass

        # Try repaired JSON
        try:
            repaired = _repair_json_string(s)
            return json.loads(repaired)
        except Exception:
            pass

        # Try python literal (single quotes, etc.)
        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, (dict, list)):
                return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            pass

        # Try python literal after repair
        try:
            repaired = _repair_json_string(s)
            obj = ast.literal_eval(repaired)
            if isinstance(obj, (dict, list)):
                return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            continue

    return None
