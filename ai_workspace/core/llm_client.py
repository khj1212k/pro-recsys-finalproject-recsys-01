# LLM API 클라이언트
# - 요청 제한, 재시도 로직 포함

import os
import json
import time
import logging
import threading
import requests
from typing import Dict, List, Optional, Any
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 10  # 최대 재생성 횟수 제한
INITIAL_BACKOFF = 1.0  # 초기 생성 대기 시간
MAX_BACKOFF = 60.0  # 최대 대기 시간
BACKOFF_MULTIPLIER = 2.0  # 대기 시간 증가 비율


class SimpleRateLimiter:
    def __init__(self, min_interval: float = 0.0):
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_ts
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_ts = time.monotonic()


class BaseLLMClient(ABC):

    @abstractmethod
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
        pass


V3_MODELS = {"HCX-005", "HCX-007", "HCX-DASH-002"}
V1_MODELS = {"HCX-003", "HCX-DASH-001", "HCX-002"}


class NaverHyperCLOVAClient(BaseLLMClient):
    """
    네이버 하이퍼클로바 X (HyperCLOVA X) API 클라이언트
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
        self.rate_limiter = SimpleRateLimiter(
            float(os.getenv("NAVER_LLM_MIN_INTERVAL", os.getenv("LLM_MIN_INTERVAL", "1.0")))
        )

        if not self.api_key:
            raise ValueError("NCP_CLOVASTUDIO_API_KEY 환경변수를 설정하세요")

        self.use_apps_auth = self.api_key.startswith("nv-")

        if not self.use_apps_auth and not self.apigw_key:
            raise ValueError("NCP_APIGW_API_KEY 환경변수를 설정하세요 (Legacy 인증 방식)")

        self.api_version = "v3" if self.model in V3_MODELS else "v1"

        self.base_url = f"https://clovastudio.stream.ntruss.com/testapp/{self.api_version}/chat-completions"

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
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

        if self.api_version == "v3":
            payload = {
                "messages": messages,
                "topP": 0.8,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "repeatPenalty": 1.2,
            }
            if self.model == "HCX-007":
                payload["thinking"] = {"effort": "none"}
        else:
            payload = {
                "messages": messages,
                "topP": 0.8,
                "topK": 0,
                "maxTokens": max_tokens,
                "temperature": temperature,
                "repeatPenalty": 1.2,
                "stopBefore": [],
                "includeAiFilters": False,
                "seed": 0
            }

        url = f"{self.base_url}/{self.model}"

        backoff = INITIAL_BACKOFF
        attempt = 0

        while True: 
            attempt += 1
            try:
                self.rate_limiter.wait()
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=120
                )

                if response.status_code == 429:
                    retry_after = response.headers.get('Retry-After')
                    wait_time = float(retry_after) if retry_after else backoff
                    logger.warning("Rate limited (429). Waiting %.1fs before retry #%s", wait_time, attempt)
                    time.sleep(wait_time)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                response.raise_for_status()
                result = response.json()

                if result.get("status", {}).get("code") == "20000":
                    content = result.get("result", {}).get("message", {}).get("content", "")
                    return content
                else:
                    error_msg = result.get("status", {}).get("message", "Unknown error")
                    error_code = result.get("status", {}).get("code", "")

                    if error_code.startswith("5"):
                        logger.warning("Server error (%s): %s. Retrying #%s...", error_code, error_msg, attempt)
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue

                    logger.error("HyperCLOVA API client error (%s): %s", error_code, error_msg)
                    return None

            except requests.exceptions.Timeout:
                logger.warning("Request timeout. Retrying #%s...", attempt)
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except requests.exceptions.RequestException as e:
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if status_code == 429 or status_code >= 500:
                        logger.warning("HTTP %s. Retrying #%s...", status_code, attempt)
                        time.sleep(backoff)
                        backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                        continue
                    logger.error("HTTP %s error. Response body: %s", status_code, e.response.text[:500])
                    return None

                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)):
                    logger.warning("Connection error. Retrying #%s...", attempt)
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.error("HyperCLOVA API request failed: %s", e)
                return None

            except Exception as e:
                logger.error("HyperCLOVA API unexpected error: %s", e)
                return None


class OpenAIClient(BaseLLMClient):

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
        self.rate_limiter = SimpleRateLimiter(
            float(os.getenv("OPENAI_LLM_MIN_INTERVAL", os.getenv("LLM_MIN_INTERVAL", "0.3")))
        )

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> Optional[str]:
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
                self.rate_limiter.wait()
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content

            except Exception as e:
                last_error = str(e)
                error_str = str(e).lower()

                if "rate_limit" in error_str or "429" in error_str or "500" in error_str or "503" in error_str:
                    logger.warning("OpenAI API error. Retrying %s/%s...", attempt + 1, MAX_RETRIES)
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.error("OpenAI API error: %s", e)
                return None

        logger.error("Max retries (%s) exhausted. Last error: %s", MAX_RETRIES, last_error)
        return None


def get_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    if provider is None:
        provider = os.getenv("LLM_PROVIDER", "naver").lower()

    if provider == "naver" or provider == "hyperclova":
        return NaverHyperCLOVAClient()
    elif provider == "openai":
        return OpenAIClient()
    else:
        return NaverHyperCLOVAClient()


def extract_json_from_response(content: str) -> Optional[Dict]:
    
    # LLM 응답에서 JSON 추출 (복구 시도 포함)
    # 마크다운 코드블록 처리
    # 문자열 내부 개행/따옴표 복구 시도
    # 단일따옴표/파이썬 dict 형태 보정 시도
    #
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

    try:
        return json.loads(content)
    except Exception:
        candidates.append(content)

    patterns = [
        r'```json\s*([\s\S]*?)\s*```',
        r'```\s*([\s\S]*?)\s*```',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            candidates.append(match.group(1))

    balanced = _find_balanced_json(content)
    if balanced:
        candidates.append(balanced)

    brace_match = re.search(r'\{[\s\S]*\}', content)
    if brace_match:
        candidates.append(brace_match.group(0))

    for raw in candidates:
        if not raw:
            continue
        s = _clean(raw)
        if not s:
            continue
        try:
            return json.loads(s)
        except Exception:
            pass

        try:
            repaired = _repair_json_string(s)
            return json.loads(repaired)
        except Exception:
            pass

        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, (dict, list)):
                return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            pass

        try:
            repaired = _repair_json_string(s)
            obj = ast.literal_eval(repaired)
            if isinstance(obj, (dict, list)):
                return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            continue

    return None
