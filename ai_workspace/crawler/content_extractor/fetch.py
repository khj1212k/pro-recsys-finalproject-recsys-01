# 기사 페이지 다운로드 (ADR 0026 결정 9)
# - 요청 1번마다 시간 상한(연결·첫 응답·본문 전체)을 두고, 라이브러리 안의 숨은 재시도와 대기를 쓰지 않는다
# - 실패하면 원인 분류(시간 초과·연결 끊김·HTTP 상태 등)를 돌려줘 job_runs.stats에 언론사별 개수로 남긴다
#
# 예전에는 trafilatura.fetch_url을 그대로 썼다. trafilatura 2.2.0은 urllib3 Retry(total=2, backoff_factor=15)와
# 30초 타임아웃을 숨겨 두어, 응답 없이 끊거나 붙잡아 두는 서버에는 호출 1번이 HTTP 요청 3번과 30초 대기가 됐다.
# 그 위에 우리 재시도 3번이 얹혀 실패하는 기사 1건이 워커 하나를 약 4분씩 붙잡았다(증거 10). 실패 원인은
# trafilatura 로거(NullHandler)로만 나가 spawn 워커에서는 어디에도 남지 않았다.
#
# 요청 헤더(User-Agent 포함)와 바이트 -> 문자열 디코딩은 trafilatura의 것을 그대로 써서, 성공한 페이지에서
# 뽑히는 본문(과 본문 해시)이 예전과 같게 한다. 차단을 피하려고 User-Agent를 바꾸거나 다른 주소로 우회하지 않는다.

import time
from typing import Callable, Dict, NamedTuple, Optional, Tuple
from urllib.parse import urljoin

import urllib3
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NewConnectionError,
    ProtocolError,
    ReadTimeoutError,
    ResponseError,
    SSLError,
)

from config.settings import Settings

# trafilatura 기본 설정(settings.cfg)과 같은 값 - 예전 동작과 같은 페이지만 받아들인다.
MAX_REDIRECTS = 2
MIN_PAGE_BYTES = 10
MAX_PAGE_BYTES = 20_000_000
_CHUNK_BYTES = 2 ** 16

# 다시 보내도 결과가 같을 실패 - 같은 실행 안에서 다시 시도하지 않는다(다음 실행의 재시도 상한은 그대로 적용).
_FINAL_CAUSES = frozenset({"too_large", "too_small", "redirects"})
_RETRYABLE_4XX = frozenset({408, 425, 429})

_POOLS: Dict[bool, urllib3.PoolManager] = {}


class FetchOutcome(NamedTuple):
    html: Optional[str]
    cause: str = ""  # 성공이면 "" - 실패 원인 분류는 classify_error와 fetch_page_once 참고
    attempts: int = 1


def _request_headers() -> Dict[str, str]:
    from trafilatura.downloads import DEFAULT_HEADERS

    return dict(DEFAULT_HEADERS)


def _decode(data: bytes) -> str:
    from trafilatura.utils import decode_file

    return decode_file(data)


def _pool(verify: bool) -> urllib3.PoolManager:
    # 프로세스(spawn 워커)마다 하나 - 같은 언론사 기사끼리는 연결을 재사용한다(trafilatura와 같다).
    pool = _POOLS.get(verify)
    if pool is None:
        if verify:
            import certifi

            pool = urllib3.PoolManager(num_pools=50, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())
        else:
            pool = urllib3.PoolManager(num_pools=50, cert_reqs="CERT_NONE")
        _POOLS[verify] = pool
    return pool


# 연결·읽기 오류는 다시 보내지 않고(connect=0, read=0) 그대로 올린다. total=None이라 상태 코드(429·5xx)로 다시
# 보내지도 않는다 - Retry-After를 따라 몇 분씩 잠드는 일이 없다. 리다이렉트는 urllib3에 맡기지 않고(redirect=False)
# fetch_page_once가 trafilatura처럼 2번까지 따라간다 - urllib3는 홉마다 시간 상한을 새로 시작하기 때문이다.
_RETRY = urllib3.util.Retry(total=None, connect=0, read=0, redirect=False, status=0, other=0)


def classify_error(err: BaseException) -> str:
    """urllib3/소켓 예외를 실패 원인 분류 하나로 바꾼다. MaxRetryError는 감싼 원인으로 본다."""
    if isinstance(err, MaxRetryError) and err.reason is not None:
        err = err.reason
    # NewConnectionError는 하위 호환 때문에 ConnectTimeoutError의 하위 클래스다 - 먼저 본다.
    if isinstance(err, NewConnectionError):  # DNS 실패(NameResolutionError)·연결 거부
        return "connect_error"
    if isinstance(err, ConnectTimeoutError):
        return "connect_timeout"
    if isinstance(err, ReadTimeoutError):
        return "read_timeout"
    if isinstance(err, SSLError):
        return "ssl"
    if isinstance(err, ProtocolError):  # 응답 없이 끊음(RemoteDisconnected)·연결 재설정·본문 도중 끊김
        return "disconnect"
    if isinstance(err, ResponseError):  # 리다이렉트 상한 초과
        return "redirects"
    if isinstance(err, TimeoutError):
        return "read_timeout"
    if isinstance(err, OSError):
        return "network_error"
    return "other"


def fetch_page_once(
    url: str,
    *,
    connect_timeout_s: Optional[float] = None,
    read_timeout_s: Optional[float] = None,
    deadline_s: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    _verify: bool = True,
) -> Tuple[Optional[str], str]:
    """HTTP GET 한 번. (html, "") 또는 (None, 원인)을 돌려준다.

    - 연결은 connect_timeout_s, 소켓 읽기 한 번은 read_timeout_s 안에 끝나야 한다.
    - 요청 전체(리다이렉트를 따라간 홉까지 포함한 연결·응답 헤더·본문)가 deadline_s를 넘기면 "deadline"으로 끊는다
      (조금씩 흘려보내는 서버 대비). 홉마다 남은 시간만 준다. 본문은 read1로 받아 데이터가 조금이라도 오면 바로
      돌아오고, 읽기마다 소켓 시간 제한을 남은 시간으로 줄인다.
    - 인증서 오류면 검증 없이 한 번 더 받는다 - trafilatura가 하던 대로다(공개 기사 페이지만 받는다).
    """
    connect_timeout_s = Settings.EXTRACT_CONNECT_TIMEOUT_S if connect_timeout_s is None else connect_timeout_s
    read_timeout_s = Settings.EXTRACT_READ_TIMEOUT_S if read_timeout_s is None else read_timeout_s
    deadline_s = Settings.EXTRACT_REQUEST_DEADLINE_S if deadline_s is None else deadline_s
    started = clock()
    current, redirects = url, 0
    while True:
        remaining = deadline_s - (clock() - started)
        if remaining <= 0:
            return None, "deadline"
        try:
            response = _pool(_verify).request(
                "GET",
                current,
                headers=_request_headers(),
                retries=_RETRY,
                redirect=False,
                timeout=urllib3.Timeout(total=remaining, connect=connect_timeout_s, read=read_timeout_s),
                preload_content=False,
            )
        except Exception as err:  # noqa: BLE001 - 원인 분류로 돌려준다
            cause = _past_deadline(classify_error(err), clock() - started, deadline_s)
            if cause == "ssl" and _verify:
                # trafilatura처럼 처음 주소부터 검증 없이 다시 받는다 - 남은 시간 안에서만
                return fetch_page_once(
                    url, connect_timeout_s=connect_timeout_s, read_timeout_s=read_timeout_s,
                    deadline_s=max(0.0, deadline_s - (clock() - started)), clock=clock, _verify=False,
                )
            return None, cause
        location = response.get_redirect_location()
        if not location:
            break
        # 리다이렉트 응답의 본문은 읽지 않고 연결을 닫는다(urllib3는 다 읽어 비웠다 - 흘려보내는 서버면 상한이 없다)
        response.close()
        response.release_conn()
        if redirects >= MAX_REDIRECTS:
            return None, "redirects"
        redirects += 1
        current = urljoin(current, location)

    complete = False
    try:
        if response.status != 200:
            return None, f"http_{response.status}"
        data = bytearray()
        reader = getattr(response, "read1", None) or response.read
        while True:
            remaining = deadline_s - (clock() - started)
            if remaining <= 0:
                return None, "deadline"
            _limit_socket_wait(response, min(read_timeout_s, remaining))
            chunk = reader(_CHUNK_BYTES)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_PAGE_BYTES:
                return None, "too_large"
        complete = True
    except Exception as err:  # noqa: BLE001
        return None, _past_deadline(classify_error(err), clock() - started, deadline_s)
    finally:
        if not complete:
            response.close()  # 본문을 다 읽지 않은 연결은 풀에 되돌려 재사용하지 않는다
        response.release_conn()

    if len(data) < MIN_PAGE_BYTES:
        return None, "too_small"
    return _decode(bytes(data)), ""


def _limit_socket_wait(response, seconds: float) -> None:
    """다음 소켓 읽기가 seconds보다 오래 막히지 않게 한다. 연결 객체를 못 찾으면(라이브러리 내부가 바뀌면) 그대로 둔다 -
    그때도 읽기 한 번은 read_timeout_s에서 끊기므로 요청 1번의 최악은 deadline_s + read_timeout_s다."""
    sock = getattr(getattr(response, "connection", None), "sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(max(0.001, seconds))
    except OSError:
        pass


def _past_deadline(cause: str, elapsed: float, deadline_s: float) -> str:
    # 남은 시간으로 줄인 소켓 제한에 걸린 것이면 읽기 시간 초과가 아니라 요청 전체 상한이다.
    return "deadline" if cause == "read_timeout" and elapsed >= deadline_s * 0.999 else cause


def is_retryable(cause: str) -> bool:
    """같은 실행 안에서 한 번 더 보낼 만한 실패인가. 4xx(408·425·429 제외)와 크기·리다이렉트 문제는 아니다."""
    if cause in _FINAL_CAUSES:
        return False
    if cause.startswith("http_"):
        code = int(cause[5:])
        return code >= 500 or code in _RETRYABLE_4XX
    return True


# 사이트 단위 실패 - 그 언론사로 가는 다른 기사 요청도 같은 꼴이 될 만한 원인이다. 언론사별 차단기(extractor.py)는 이것만
# 센다. 없는 기사(404·410 같은 4xx), 크기·리다이렉트 상한, 분류 밖의 예외(other)는 그 기사만의 문제라 세지 않는다.
_SITE_CAUSES = frozenset({
    "connect_error", "connect_timeout", "disconnect", "read_timeout", "deadline", "ssl", "network_error",
})
_SITE_4XX = frozenset({403, 408, 425, 429})


def is_site_failure(cause: str) -> bool:
    """이 실패가 기사가 아니라 사이트(또는 출발지와 사이트 사이)의 문제를 가리키는가. 연결·읽기 실패, 5xx, 403·429 등."""
    if cause in _SITE_CAUSES:
        return True
    if cause.startswith("http_") and cause[5:].isdigit():
        code = int(cause[5:])
        return code >= 500 or code in _SITE_4XX
    return False


def fetch_page(
    url: str,
    *,
    attempts: Optional[int] = None,
    backoff_s: Optional[float] = None,
    fetch_once: Optional[Callable[[str], Tuple[Optional[str], str]]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FetchOutcome:
    """fetch_page_once를 attempts번까지(기본 Settings.EXTRACT_FETCH_ATTEMPTS) 짧은 대기로 다시 시도한다.

    fetch_once/sleep_fn은 테스트에서 네트워크와 대기 없이 검증하려고 주입할 수 있게 둔다.
    """
    attempts = max(1, Settings.EXTRACT_FETCH_ATTEMPTS if attempts is None else attempts)
    backoff_s = Settings.EXTRACT_RETRY_BACKOFF_S if backoff_s is None else backoff_s
    fetch_once = fetch_once or fetch_page_once
    cause = "other"
    for attempt in range(1, attempts + 1):
        try:
            html, cause = fetch_once(url)
        except Exception as err:  # noqa: BLE001 - 주입한 함수나 디코딩의 예외도 원인으로 센다
            html, cause = None, classify_error(err)
        if html:
            return FetchOutcome(html, "", attempt)
        cause = cause or "empty_body"
        if attempt == attempts or not is_retryable(cause):
            return FetchOutcome(None, cause, attempt)
        sleep_fn(backoff_s * (2 ** (attempt - 1)))
    return FetchOutcome(None, cause, attempts)
