import datetime as dt
import hashlib

import netperftest.netperftest as npt
from netperftest.netperftest import S3AuthHeaders


class _FrozenDatetime:
    """Freezes now() so a signature is reproducible across calls."""

    @staticmethod
    def now(tz=None):
        return dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=tz)


def _sign(monkeypatch, secret='SECRET'):
    monkeypatch.setattr(npt, 'datetime', _FrozenDatetime)
    return S3AuthHeaders(
        access='AKID', secret=secret, region='us-east-1',
        url='https://bucket.s3.amazonaws.com/key', method='get',
        headers=None, payload=b'').to_dict()


def test_signed_headers_have_expected_fields(monkeypatch):
    headers = _sign(monkeypatch)
    assert headers['X-Amz-Date'] == '20200102T030405Z'
    assert headers['Host'] == 'bucket.s3.amazonaws.com'
    assert headers['X-Amz-Content-Sha256'] == hashlib.sha256(b'').hexdigest()

    auth = headers['Authorization']
    assert auth.startswith('AWS4-HMAC-SHA256 ')
    assert 'Credential=AKID/20200102/us-east-1/s3/aws4_request' in auth
    assert 'SignedHeaders=host;x-amz-content-sha256;x-amz-date' in auth
    assert 'Signature=' in auth


def test_signature_is_deterministic(monkeypatch):
    assert _sign(monkeypatch)['Authorization'] == \
        _sign(monkeypatch)['Authorization']


def test_secret_changes_signature(monkeypatch):
    one = _sign(monkeypatch, secret='one')['Authorization']
    two = _sign(monkeypatch, secret='two')['Authorization']
    assert one != two
