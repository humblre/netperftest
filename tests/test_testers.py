import pytest

import netperftest.netperftest as npt
from netperftest.netperftest import (
    Multimeter,
    MultimeterClientError,
    MultimeterServerError,
)


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def shutdown(self, how):
        pass


class _FakeResponse:
    def __init__(self, status_code=200, reason='OK', headers=None,
                 content=b'data', chunks=None):
        self.status_code = status_code
        self.reason = reason
        self.text = 'body'
        self.headers = headers or {}
        self._content = content
        self._chunks = chunks if chunks is not None else [content]

    @property
    def content(self):
        return self._content

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def test_latency_single_run_has_zero_stdev(monkeypatch):
    monkeypatch.setattr(npt.socket, 'create_connection',
                        lambda address, timeout=None: _FakeSocket())
    ave, std = Multimeter.test_latency(host='localhost', port=80,
                                       runs=1, interval=0)
    assert ave >= 0
    assert std == 0.0


def test_latency_averages_multiple_runs(monkeypatch):
    monkeypatch.setattr(npt.socket, 'create_connection',
                        lambda address, timeout=None: _FakeSocket())
    ave, std = Multimeter.test_latency(host='localhost', port=80,
                                       runs=3, interval=0)
    assert ave >= 0
    assert std >= 0


def test_latency_connection_error_becomes_server_error(monkeypatch):
    def boom(address, timeout=None):
        raise OSError('connection refused')

    monkeypatch.setattr(npt.socket, 'create_connection', boom)
    with pytest.raises(MultimeterServerError):
        Multimeter.test_latency(host='localhost', port=80,
                                runs=1, interval=0)


def test_download_with_content_length(monkeypatch):
    response = _FakeResponse(headers={'content-length': '8'},
                             chunks=[b'abcd', b'efgh'])
    monkeypatch.setattr(npt.requests, 'get', lambda *a, **k: response)
    ave, std = Multimeter.test_download(url='http://x/y', runs=1, interval=0)
    assert ave >= 0
    assert std == 0.0


def test_download_without_content_length(monkeypatch):
    response = _FakeResponse(headers={}, content=b'hello')
    monkeypatch.setattr(npt.requests, 'get', lambda *a, **k: response)
    ave, std = Multimeter.test_download(url='http://x/y', runs=1, interval=0)
    assert ave >= 0


def test_download_client_error(monkeypatch):
    response = _FakeResponse(status_code=404, reason='Not Found')
    monkeypatch.setattr(npt.requests, 'get', lambda *a, **k: response)
    with pytest.raises(MultimeterClientError):
        Multimeter.test_download(url='http://x/y', runs=1, interval=0)


def test_upload_sets_content_length_and_returns_timing(monkeypatch):
    captured = {}

    def fake_put(url, headers=None, timeout=None, data=None):
        captured['headers'] = headers
        captured['data'] = data
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(npt.requests, 'put', fake_put)
    ave, std = Multimeter.test_upload(url='http://x/y', size=100,
                                      method='put', runs=1, interval=0)
    assert ave >= 0
    assert captured['headers']['content-length'] == '100'
    assert len(captured['data']) == 100  # Streamable.__len__


def test_upload_uses_post_when_requested(monkeypatch):
    calls = {}

    def fake_post(url, headers=None, timeout=None, data=None):
        calls['post'] = True
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(npt.requests, 'post', fake_post)
    Multimeter.test_upload(url='http://x/y', size=10, method='post',
                           runs=1, interval=0)
    assert calls.get('post')


def test_upload_server_error(monkeypatch):
    def fake_put(url, headers=None, timeout=None, data=None):
        return _FakeResponse(status_code=503, reason='Service Unavailable')

    monkeypatch.setattr(npt.requests, 'put', fake_put)
    with pytest.raises(MultimeterServerError):
        Multimeter.test_upload(url='http://x/y', size=10, method='put',
                               runs=1, interval=0)
