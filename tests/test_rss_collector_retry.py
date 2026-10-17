import sys
import os
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.rss_collector import parse_feed_with_retry


def test_parse_feed_with_retry_succeeds_after_bozo_failures():
    calls = []

    def flaky_parse(url):
        calls.append(url)
        if len(calls) < 2:
            return SimpleNamespace(bozo=1, bozo_exception=Exception("temp fail"), entries=[])
        return SimpleNamespace(bozo=0, entries=[{"title": "a"}])

    sleeps = []
    feed = parse_feed_with_retry(
        "http://example.com/rss", parse_fn=flaky_parse, max_attempts=5, sleep_fn=sleeps.append
    )

    assert feed.bozo == 0
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_parse_feed_with_retry_gives_up_after_max_attempts_and_returns_last_feed():
    def always_bozo(url):
        return SimpleNamespace(bozo=1, bozo_exception=Exception("permanent fail"), entries=[])

    sleeps = []
    feed = parse_feed_with_retry(
        "http://example.com/rss", parse_fn=always_bozo, max_attempts=3, sleep_fn=sleeps.append
    )

    assert feed.bozo == 1
    assert len(sleeps) == 2


def test_default_fetch_identifies_with_project_user_agent_not_feedparser_default(monkeypatch):
    """한국경제 RSS는 feedparser 기본 UA에 403을 준다(2026-09-26 첫 수집에서 확인) -
    기본 파서는 프로젝트 UA를 명시해서 요청해야 한다."""
    import crawler.rss_collector as rss_collector

    seen = {}

    def fake_parse(url, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(bozo=0, entries=[])

    monkeypatch.setattr(rss_collector.feedparser, "parse", fake_parse)
    parse_feed_with_retry("http://example.com/rss", max_attempts=1)

    assert seen.get("agent") == rss_collector.Settings.HTTP_USER_AGENT
    assert "feedparser" not in seen["agent"].lower()


def test_unreadable_feed_error_includes_http_status():
    from unittest.mock import MagicMock, patch

    from crawler.rss_collector import collect_rss

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,)
    blocked = SimpleNamespace(bozo=1, bozo_exception=Exception("undefined entity"), entries=[], status=403)

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"차단언론": ("direct", "http://blocked/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=blocked):
        stats = collect_rss()

    assert "HTTP 403" in stats["per_feed"]["차단언론"]["error"]


def _local_server(handler):
    """테스트 안에서만 잠깐 뜨는 127.0.0.1 HTTP 소켓 서버(외부 네트워크 없음)."""
    import socketserver
    import threading

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            raw = b""
            try:
                while b"\r\n\r\n" not in raw:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    raw += chunk
            except OSError:
                return
            handler(self.request, self.server.stop)

    class _Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.stop = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _close(srv):
    srv.stop.set()
    srv.shutdown()
    srv.server_close()


SYNTHETIC_RSS = (
    '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>합성 피드</title>'
    '<item><title>합성 기사</title><link>http://127.0.0.1/a</link>'
    '<pubDate>Sat, 17 Oct 2026 09:00:00 +0900</pubDate></item></channel></rss>'
).encode("utf-8")


def test_feed_request_that_holds_the_connection_is_cut_by_the_socket_timeout(monkeypatch):
    """feedparser는 urllib을 timeout 없이 부른다 - 응답 없이 붙잡는 RSS 서버(Tier 0의 동아일보 RSS는 첫 바이트까지
    74초가 걸린 적이 있다)에 ingest가 끝없이 멈추지 않게 소켓 대기 1번을 RSS_SOCKET_TIMEOUT_S로 묶는다(ADR 0026 결정 9)."""
    import socket
    import time

    import crawler.rss_collector as rss_collector

    srv = _local_server(lambda sock, stop: stop.wait(5))  # 요청을 읽고 아무것도 보내지 않는다
    monkeypatch.setattr(rss_collector.Settings, "RSS_SOCKET_TIMEOUT_S", 0.5)
    before = socket.getdefaulttimeout()
    try:
        started = time.monotonic()
        try:
            outcome = getattr(rss_collector._fetch_feed(f"http://127.0.0.1:{srv.server_address[1]}/rss"),
                              "bozo_exception", None)
        except Exception as err:  # noqa: BLE001 - 응답 대기 중 시간 초과는 feedparser가 잡지 않고 올린다
            outcome = err
        elapsed = time.monotonic() - started
    finally:
        _close(srv)

    assert elapsed < 3
    assert "timed out" in str(outcome)
    assert socket.getdefaulttimeout() == before  # 프로세스 기본값은 되돌려 둔다


def test_feed_is_still_parsed_with_the_socket_timeout(monkeypatch):
    import socket

    import crawler.rss_collector as rss_collector

    def serve(sock, stop):
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nContent-Length: "
                     + str(len(SYNTHETIC_RSS)).encode() + b"\r\nConnection: close\r\n\r\n" + SYNTHETIC_RSS)

    srv = _local_server(serve)
    monkeypatch.setattr(rss_collector.Settings, "RSS_SOCKET_TIMEOUT_S", 2.0)
    before = socket.getdefaulttimeout()
    try:
        feed = rss_collector._fetch_feed(f"http://127.0.0.1:{srv.server_address[1]}/rss")
    finally:
        _close(srv)

    assert not feed.bozo
    assert [e.title for e in feed.entries] == ["합성 기사"]
    assert socket.getdefaulttimeout() == before
