"""
Unified LLM Client for multiple providers
Supports: OpenAI, Naver HyperCLOVA X
"""
import os
import json
import time
import requests
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod


# Retry configuration
MAX_RETRIES = 10  # Maximum retry attempts
INITIAL_BACKOFF = 1.0  # Initial wait time in seconds
MAX_BACKOFF = 60.0  # Maximum wait time between retries
BACKOFF_MULTIPLIER = 2.0  # Exponential backoff multiplier


class BaseLLMClient(ABC):
    """Base class for LLM clients"""

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        """Generate chat completion"""
        pass


# Models that use v3 API
V3_MODELS = {"HCX-005", "HCX-007", "HCX-DASH-002"}
# Models that use v1 API
V1_MODELS = {"HCX-003", "HCX-DASH-001", "HCX-002"}


class NaverHyperCLOVAClient(BaseLLMClient):
    """
    Naver HyperCLOVA X API Client

    Supports:
    - v1 API: HCX-003, HCX-DASH-001
    - v3 API: HCX-005, HCX-007, HCX-DASH-002

    Authentication:
    - Apps method (nv- prefix): Bearer token
    - Legacy method: X-NCP-CLOVASTUDIO-API-KEY + X-NCP-APIGW-API-KEY
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
                    print(f"⏳ Rate limited (429). Waiting {wait_time:.1f}s before retry #{attempt}...")
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
                        print(f"⚠️ Server error ({error_code}): {error_msg}. Retrying #{attempt}...")
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue

                    # Client errors (4xx except 429) - give up
                    print(f"❌ HyperCLOVA API client error ({error_code}): {error_msg}")
                    return None

            except requests.exceptions.Timeout:
                print(f"⏰ Request timeout. Retrying #{attempt}...")
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except requests.exceptions.RequestException as e:
                # Check if it's a retryable error (connection error, 5xx, etc.)
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if status_code == 429 or status_code >= 500:
                        print(f"⚠️ HTTP {status_code}. Retrying #{attempt}...")
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue
                    # Client errors - give up
                    print(f"❌ HTTP {status_code} error")
                    print(f"Response body: {e.response.text[:500]}")
                    return None

                # Connection errors are retryable - 무한 재시도
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)):
                    print(f"🔌 Connection error. Retrying #{attempt}...")
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                print(f"❌ HyperCLOVA API request failed: {e}")
                return None

            except Exception as e:
                print(f"❌ HyperCLOVA API unexpected error: {e}")
                return None


class OpenAIClient(BaseLLMClient):
    """OpenAI API Client (for fallback or comparison)"""

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
                    print(f"⏳ OpenAI API error. Retrying {attempt + 1}/{MAX_RETRIES}...")
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                print(f"OpenAI API error: {e}")
                return None

        print(f"❌ Max retries ({MAX_RETRIES}) exhausted. Last error: {last_error}")
        return None


def get_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    """
    Factory function to get appropriate LLM client

    Args:
        provider: 'naver', 'openai', or None (auto-detect from env)

    Returns:
        LLM client instance
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
    Extract JSON from LLM response (handles markdown code blocks)

    Args:
        content: Raw LLM response content

    Returns:
        Parsed JSON dict or None
    """
    if not content:
        return None

    # Try direct JSON parse first
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Try to extract from markdown code block
    import re

    # Match ```json ... ``` or ``` ... ```
    patterns = [
        r'```json\s*([\s\S]*?)\s*```',
        r'```\s*([\s\S]*?)\s*```',
        r'\{[\s\S]*\}'  # Raw JSON object
    ]

    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            try:
                json_str = match.group(1) if '```' in pattern else match.group(0)
                return json.loads(json_str)
            except (json.JSONDecodeError, IndexError):
                continue

    return None
