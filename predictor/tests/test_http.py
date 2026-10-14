import os

from footy.data.http import HttpClient, HttpError


def _fetcher(script):
    """script — список (status, data, retry_after); отдаёт по очереди, фиксируя вызовы."""
    calls = {"n": 0}

    def fetch(url, headers, params, timeout):
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        return script[i]

    return fetch, calls


def test_success_returns_json_and_caches(tmp_path):
    fetch, calls = _fetcher([(200, {"ok": 1}, None)])
    c = HttpClient(cache_dir=str(tmp_path), fetch=fetch, sleep=lambda s: None)
    assert c.get_json("http://x/a", params={"p": 1}) == {"ok": 1}
    # второй вызов обслуживается из кэша — сеть не трогается
    assert c.get_json("http://x/a", params={"p": 1}) == {"ok": 1}
    assert calls["n"] == 1


def test_cache_disabled_always_fetches(tmp_path):
    fetch, calls = _fetcher([(200, {"v": 1}, None), (200, {"v": 2}, None)])
    c = HttpClient(cache_dir=None, fetch=fetch, sleep=lambda s: None)
    assert c.get_json("http://x/a")["v"] == 1
    assert c.get_json("http://x/a")["v"] == 2
    assert calls["n"] == 2


def test_ttl_expiry_refetches(tmp_path):
    fetch, calls = _fetcher([(200, {"v": 1}, None), (200, {"v": 2}, None)])
    c = HttpClient(cache_dir=str(tmp_path), ttl=0.0, fetch=fetch,
                   sleep=lambda s: None)
    # ttl=0 => кэш сразу «протух», всегда свежий запрос
    assert c.get_json("http://x/a")["v"] == 1
    assert c.get_json("http://x/a")["v"] == 2
    assert calls["n"] == 2


def test_retries_on_429_then_succeeds():
    slept = []
    fetch, calls = _fetcher([(429, None, None), (429, None, None),
                             (200, {"ok": 1}, None)])
    c = HttpClient(cache_dir=None, retries=4, backoff=1.0, fetch=fetch,
                   sleep=slept.append)
    assert c.get_json("http://x/a") == {"ok": 1}
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]  # экспоненциальный backoff


def test_retry_after_header_respected():
    slept = []
    fetch, _ = _fetcher([(429, None, 7.5), (200, {"ok": 1}, None)])
    c = HttpClient(cache_dir=None, retries=2, backoff=1.0, fetch=fetch,
                   sleep=slept.append)
    c.get_json("http://x/a")
    assert slept == [7.5]  # пауза из Retry-After, не из backoff


def test_gives_up_after_retries():
    fetch, calls = _fetcher([(429, None, None)])
    c = HttpClient(cache_dir=None, retries=2, backoff=0.1, fetch=fetch,
                   sleep=lambda s: None)
    try:
        c.get_json("http://x/a")
    except HttpError:
        assert calls["n"] == 3  # 1 + 2 ретрая
        return
    assert False, "должно было упасть HttpError"


def test_client_error_not_retried():
    fetch, calls = _fetcher([(404, None, None)])
    c = HttpClient(cache_dir=None, retries=3, fetch=fetch, sleep=lambda s: None)
    try:
        c.get_json("http://x/a")
    except HttpError:
        assert calls["n"] == 1  # 4xx (кроме 429) не ретраим
        return
    assert False
