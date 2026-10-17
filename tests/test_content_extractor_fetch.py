"""기사 페이지 다운로드(crawler/content_extractor/fetch.py, ADR 0026 결정 9).

실제 소켓 동작(응답 없이 끊기, 붙잡아 두기, 조금씩 흘려보내기, 리다이렉트, 압축)은 테스트 안에서만 잠깐 뜨는
127.0.0.1 소켓 서버로 확인한다. 페이지는 모두 합성 HTML이다. 외부 네트워크는 쓰지 않는다.
"""
import gzip
import os
import socket
import socketserver
import sys
import threading
import time

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor import fetch  # noqa: E402
from crawler.content_extractor.fetch import (  # noqa: E402
    FetchOutcome,
    classify_error,
    fetch_page,
    fetch_page_once,
    is_retryable,
)

SYNTHETIC_HTML = (
    "<html><head><meta charset='utf-8'><title>합성 기사</title></head><body><article>"
    + "<p>합성 문단입니다. 실제 기사가 아닙니다.</p>" * 20
    + "</article></body></html>"
)
EUCKR_HTML = "<html><head><meta charset='euc-kr'></head><body><p>" + "가나다라 합성 본문 " * 30 + "</p></body></html>"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):  # noqa: C901 - 경로별 동작표
        sock = self.request
        sock.settimeout(5)
        raw = b""
        try:
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                raw += chunk
        except OSError:
            return
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        request_line, *header_lines = head.split("\r\n")
        path = request_line.split()[1]
        headers = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in header_lines if ":" in h)}
        self.server.seen.append((path, headers))
        stop = self.server.stop

        def respond(status, body=b"", extra=()):
            lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close", *extra]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

        if path == "/ok":
            respond("200 OK", SYNTHETIC_HTML.encode("utf-8"), ["Content-Type: text/html; charset=utf-8"])
        elif path == "/gzip":
            respond("200 OK", gzip.compress(SYNTHETIC_HTML.encode("utf-8")),
                    ["Content-Type: text/html", "Content-Encoding: gzip"])
        elif path == "/euckr":
            respond("200 OK", EUCKR_HTML.encode("euc-kr"), ["Content-Type: text/html"])
        elif path.startswith("/status/"):
            code = int(path.rsplit("/", 1)[1])
            respond(f"{code} X", b"<html>blocked</html>", ["Retry-After: 120"])
        elif path == "/disconnect":
            return  # 요청을 읽고 응답 없이 닫는다(관측된 "Empty reply from server")
        elif path == "/hold":
            stop.wait(3)  # 연결은 받고 아무것도 보내지 않는다(관측된 0바이트 붙잡기)
        elif path == "/drip":
            body = b"x" * 400
            sock.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode())
            for i in range(len(body)):
                if stop.wait(0.02):
                    return
                try:
                    sock.sendall(body[i:i + 1])
                except OSError:
                    return
        elif path == "/hold-body":
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n<html>")
            stop.wait(3)  # 헤더와 본문 앞부분만 보내고 멈춘다
        elif path.startswith("/redirect/"):
            n = int(path.rsplit("/", 1)[1])
            respond("302 Found", b"", [f"Location: {'/ok' if n == 0 else f'/redirect/{n - 1}'}"])
        elif path == "/tiny":
            respond("200 OK", b"<p/>", ["Content-Type: text/html"])
        elif path == "/big":
            respond("200 OK", b"<p>" + b"x" * 5000 + b"</p>", ["Content-Type: text/html"])
        else:
            respond("404 Not Found", b"")


@pytest.fixture(scope="module")
def server():
    class _Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.seen = []
    srv.stop = threading.Event()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", srv
    finally:
        srv.stop.set()
        srv.shutdown()
        srv.server_close()


def _once(url, **kw):
    kw.setdefault("connect_timeout_s", 1.0)
    kw.setdefault("read_timeout_s", 1.0)
    kw.setdefault("deadline_s", 2.0)
    return fetch_page_once(url, **kw)


def test_ok_page_is_decoded_like_trafilatura_and_sends_trafilatura_headers(server):
    """성공한 페이지의 문자열(=본문 추출 입력)과 요청 헤더는 trafilatura.fetch_url 때와 같아야 한다."""
    from trafilatura.downloads import DEFAULT_HEADERS
    from trafilatura.utils import decode_file

    base, srv = server
    html, cause = _once(f"{base}/ok")
    assert cause == "" and html == decode_file(SYNTHETIC_HTML.encode("utf-8"))
    _, headers = srv.seen[-1]
    assert headers["user-agent"] == DEFAULT_HEADERS["User-Agent"]
    assert headers["user-agent"].startswith("trafilatura/")  # 차단을 피하려고 UA를 바꾸지 않는다

    html, cause = _once(f"{base}/gzip")
    assert cause == "" and html == decode_file(SYNTHETIC_HTML.encode("utf-8"))
    html, cause = _once(f"{base}/euckr")
    assert cause == "" and html == decode_file(EUCKR_HTML.encode("euc-kr")) and "가나다라" in html


def test_disconnect_without_response_is_one_quick_request(server):
    base, srv = server
    before = len(srv.seen)
    started = time.monotonic()
    assert _once(f"{base}/disconnect") == (None, "disconnect")
    assert time.monotonic() - started < 1.0
    assert len(srv.seen) - before == 1  # 라이브러리 안에서 다시 보내지 않는다(trafilatura는 3번 보냈다)


def test_server_that_holds_the_connection_hits_the_read_timeout_once(server):
    base, srv = server
    before = len(srv.seen)
    started = time.monotonic()
    assert _once(f"{base}/hold", read_timeout_s=0.3) == (None, "read_timeout")
    assert time.monotonic() - started < 1.5
    assert len(srv.seen) - before == 1


def test_slow_drip_body_is_cut_at_the_request_deadline(server):
    """소켓 읽기마다 데이터가 조금씩 오면 읽기 타임아웃에 안 걸린다 - 요청 전체 상한이 끊는다."""
    base, _ = server
    started = time.monotonic()
    assert _once(f"{base}/drip", read_timeout_s=1.0, deadline_s=0.3) == (None, "deadline")
    assert time.monotonic() - started < 1.5


def test_body_stall_is_cut_at_the_request_deadline_not_after_another_full_read_timeout(server):
    """본문 도중 멈추면 남은 시간만큼만 기다린다 - 요청 1번이 상한(deadline_s)을 크게 넘기지 않는다."""
    base, _ = server
    started = time.monotonic()
    assert _once(f"{base}/hold-body", read_timeout_s=2.0, deadline_s=0.4) == (None, "deadline")
    assert time.monotonic() - started < 1.0


def test_header_wait_is_also_bounded_by_the_request_deadline(server):
    base, _ = server
    started = time.monotonic()
    assert _once(f"{base}/hold", read_timeout_s=2.0, deadline_s=0.4) == (None, "deadline")
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("code", [403, 404, 429, 503])
def test_non_200_status_is_reported_without_retry_after_sleep(server, code):
    """429/503도 Retry-After(120초)를 따라 잠들거나 다시 보내지 않는다 - 다시 보낼지는 fetch_page가 정한다."""
    base, srv = server
    before = len(srv.seen)
    started = time.monotonic()
    assert _once(f"{base}/status/{code}") == (None, f"http_{code}")
    assert time.monotonic() - started < 1.0
    assert len(srv.seen) - before == 1


def test_redirects_are_followed_up_to_two_like_trafilatura(server):
    base, _ = server
    html, cause = _once(f"{base}/redirect/1")  # /redirect/1 -> /redirect/0 -> /ok: 리다이렉트 2번
    assert cause == "" and "합성 문단" in html
    assert _once(f"{base}/redirect/2") == (None, "redirects")  # 3번


def test_size_limits(server, monkeypatch):
    base, _ = server
    assert _once(f"{base}/tiny") == (None, "too_small")
    monkeypatch.setattr(fetch, "MAX_PAGE_BYTES", 1000)
    assert _once(f"{base}/big") == (None, "too_large")


def test_refused_connection_is_a_connect_error():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 아무도 듣지 않는 포트
    assert fetch_page_once(f"http://127.0.0.1:{port}/x", connect_timeout_s=1.0, read_timeout_s=1.0,
                           deadline_s=2.0) == (None, "connect_error")


def test_classify_error_maps_urllib3_errors():
    from urllib3.exceptions import (ConnectTimeoutError, MaxRetryError, NameResolutionError, NewConnectionError,
                                    ProtocolError, ReadTimeoutError, ResponseError, SSLError)

    assert classify_error(MaxRetryError(None, "u", ProtocolError("Connection aborted.", ConnectionResetError()))) \
        == "disconnect"
    assert classify_error(MaxRetryError(None, "u", ReadTimeoutError(None, "u", "timed out"))) == "read_timeout"
    assert classify_error(MaxRetryError(None, "u", ConnectTimeoutError("timed out"))) == "connect_timeout"
    # NewConnectionError는 ConnectTimeoutError의 하위 클래스지만 시간 초과가 아니다
    assert classify_error(MaxRetryError(None, "u", NewConnectionError(None, "refused"))) == "connect_error"
    assert classify_error(NameResolutionError("h", None, socket.gaierror())) == "connect_error"
    assert classify_error(MaxRetryError(None, "u", SSLError("bad cert"))) == "ssl"
    assert classify_error(MaxRetryError(None, "u", ResponseError("too many redirects"))) == "redirects"
    assert classify_error(TimeoutError()) == "read_timeout"
    assert classify_error(ConnectionResetError()) == "network_error"
    assert classify_error(ValueError("x")) == "other"


@pytest.mark.parametrize("cause,expected", [
    ("disconnect", True), ("read_timeout", True), ("connect_timeout", True), ("deadline", True),
    ("connect_error", True), ("http_500", True), ("http_503", True), ("http_429", True), ("http_408", True),
    ("http_403", False), ("http_404", False), ("http_410", False),
    ("too_small", False), ("too_large", False), ("redirects", False),
])
def test_is_retryable(cause, expected):
    assert is_retryable(cause) is expected


def test_fetch_page_retries_transient_failures_with_short_backoff():
    calls, sleeps = [], []

    def flaky(url):
        calls.append(url)
        return (None, "disconnect") if len(calls) < 2 else ("<html>ok</html>", "")

    assert fetch_page("u", attempts=2, backoff_s=2, fetch_once=flaky, sleep_fn=sleeps.append) \
        == FetchOutcome("<html>ok</html>", "", 2)
    assert sleeps == [2]


def test_fetch_page_gives_up_after_attempts_and_reports_last_cause():
    sleeps = []
    causes = iter(["read_timeout", "disconnect", "disconnect"])
    outcome = fetch_page("u", attempts=3, backoff_s=2, fetch_once=lambda u: (None, next(causes)),
                         sleep_fn=sleeps.append)
    assert outcome == FetchOutcome(None, "disconnect", 3)
    assert sleeps == [2, 4]  # 마지막 시도 뒤에는 기다리지 않는다


def test_fetch_page_does_not_retry_final_failures():
    sleeps = []
    outcome = fetch_page("u", attempts=3, backoff_s=2, fetch_once=lambda u: (None, "http_404"), sleep_fn=sleeps.append)
    assert outcome == FetchOutcome(None, "http_404", 1) and sleeps == []


def test_fetch_page_counts_exceptions_from_the_fetcher_as_causes():
    def boom(url):
        raise ConnectionResetError()

    assert fetch_page("u", attempts=1, fetch_once=boom, sleep_fn=lambda s: None) == FetchOutcome(None, "network_error", 1)


def test_fetch_page_defaults_bound_the_cost_of_a_failing_article():
    """기본값으로 응답 없이 끊는 페이지 1건에 드는 최악 시간: 요청 2번 x 30초 + 대기 2초.
    예전 trafilatura.fetch_url x 3은 붙잡는 서버에 요청 9번(각 30초) + urllib3 대기 3 x 30초 + 우리 대기 3초였다."""
    from config.settings import Settings

    assert Settings.EXTRACT_FETCH_ATTEMPTS == 2
    worst = (Settings.EXTRACT_FETCH_ATTEMPTS * Settings.EXTRACT_REQUEST_DEADLINE_S
             + Settings.EXTRACT_RETRY_BACKOFF_S * (2 ** (Settings.EXTRACT_FETCH_ATTEMPTS - 1) - 1))
    assert worst <= 62
    assert Settings.EXTRACT_CONNECT_TIMEOUT_S + Settings.EXTRACT_READ_TIMEOUT_S <= Settings.EXTRACT_REQUEST_DEADLINE_S
