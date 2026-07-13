#!/usr/bin/env python3
"""Measure latency and upload/download speeds for a single destination.

No parallelism is used, so a single connection has exclusive use of the
available bandwidth for the duration of a measurement. Each test is repeated
a configurable number of times and reported as an average and a standard
deviation.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import socket
import sys
import urllib.parse
from argparse import ArgumentParser
from datetime import datetime, timezone
from json import loads
from statistics import mean, stdev
from time import perf_counter, sleep

import requests

__version__ = '0.0.1'

CHUNK_SIZE = 1 << 13
MAX_RETRIES = 10
TEST_TYPES = ('latency', 'download', 'upload')

DEBUG = False


class MultimeterException(Exception):
    """Base error, optionally carrying an HTTP status and response body."""

    def __init__(self, status_int: int | None = None,
                 message: str = '', content: str = '') -> None:
        self.status_int = status_int
        self.content = content
        self.message = f'{status_int} {message}' if status_int else message
        super().__init__(self.message)

    def __str__(self) -> str:
        return (f'\033[1m\033[31m{self.message}\033[0m\n\n'
                f'\033[31m{self.content}\033[0m\n')


class MultimeterClientError(MultimeterException):
    """Raised for HTTP 4xx responses."""


class MultimeterServerError(MultimeterException):
    """Raised for HTTP 5xx responses and for connection failures."""


class MultimeterUsageError(MultimeterException):
    """Raised when required arguments are missing or invalid."""


class Streamable:
    """A run of zero bytes, produced in chunks.

    Used as an upload body so the payload is generated on the fly instead of
    being built in memory. Re-iterable on purpose: requests may read the body
    more than once when it retries.
    """

    def __init__(self, total_length: int,
                 chunk_size: int = CHUNK_SIZE) -> None:
        self.total_length = total_length
        self.chunk_size = chunk_size

    def __len__(self) -> int:
        return self.total_length

    def __iter__(self):
        remaining = self.total_length
        while remaining > 0:
            size = min(self.chunk_size, remaining)
            remaining -= size
            if DEBUG:
                sent = self.total_length - remaining
                sys.stderr.write(f'{sent * 100 / self.total_length:6.2f} %\r')
            yield bytes(size)


class AWSAuthHeaders:
    """Build request headers signed with AWS Signature Version 4."""

    algorithm = 'AWS4-HMAC-SHA256'
    aws4_request = 'aws4_request'
    service = ''  # set by subclasses

    def to_dict(self) -> dict:
        return self._headers

    def __init__(self, access, secret, region, url, method, headers, payload):
        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        now = datetime.now(timezone.utc)
        ymdhms = now.strftime('%Y%m%dT%H%M%SZ')
        ymd = now.strftime('%Y%m%d')

        payload_hash = hashlib.sha256(payload).hexdigest()
        credential_scope = f'{ymd}/{region}/{self.service}/{self.aws4_request}'

        parsed_url = urllib.parse.urlparse(url)
        path = parsed_url.path or '/'

        self._headers = {} if headers is None else headers.copy()
        self._headers['X-Amz-Date'] = ymdhms
        self._headers['X-Amz-Content-Sha256'] = payload_hash
        self._headers['Host'] = parsed_url.netloc

        # SigV4 sorts headers by lower-cased name. Build the canonical block
        # and the signed list from the same pairs so they stay in sync.
        lowered = sorted((k.lower(), v) for k, v in self._headers.items())
        canonical_headers = ''.join(f'{name}:{value}\n'
                                    for name, value in lowered)
        signed_headers = ';'.join(name for name, _ in lowered)

        canonical_request = '\n'.join([
            method.upper(), path, parsed_url.query,
            canonical_headers, signed_headers, payload_hash])

        string_to_sign = '\n'.join([
            self.algorithm, ymdhms, credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest()])

        signing_key = sign(
            sign(sign(sign(f'AWS4{secret}'.encode(), ymd), region),
                 self.service),
            self.aws4_request)
        signature = hmac.new(signing_key, string_to_sign.encode(),
                             hashlib.sha256).hexdigest()

        self._headers['Authorization'] = (
            f'{self.algorithm} '
            f'Credential={access}/{credential_scope}, '
            f'SignedHeaders={signed_headers}, '
            f'Signature={signature}')


class S3AuthHeaders(AWSAuthHeaders):
    """AWS SigV4 headers for the S3 service."""

    service = 's3'


def _overwrite(previous: str, text: str) -> str:
    """Redraw the status line, padding to erase what was there before.

    Returns the text so it can be passed back as `previous` next time.
    """
    sys.stderr.write(f'{text:<{len(previous)}}\r')
    return text


def _write_progress(begin: float, done_bytes: int, total_bytes: int,
                    units: tuple[str, int]) -> None:
    """Render a download progress bar with the current transfer rate."""
    unit_name, unit_factor = units
    elapsed = perf_counter() - begin
    filled = int(40 * done_bytes / total_bytes)
    rate = done_bytes * unit_factor / elapsed / 1e6
    bar = '=' * filled + ' ' * (40 - filled)
    sys.stderr.write(f'[{bar}] {rate:.3f} Mega{unit_name}s/s\r')


class BaseTester:
    """Retry and averaging logic shared by the testers."""

    @classmethod
    def _measure(cls, func, *args, interval: float = 0.1, runs: int = 2,
                 timeout: float = 10.0, **kwargs) -> tuple[float, float]:
        results: list[float] = []
        line = ''
        for i in range(runs):
            last_error: MultimeterException | None = None
            for retry in range(MAX_RETRIES):
                if retry == 0:
                    label = func.__name__.removeprefix('_test_')
                    line = _overwrite(line,
                                      f'> {label} in progress, run {i + 1}')
                try:
                    result = func(*args, timeout=timeout, **kwargs)
                except MultimeterException as exc:
                    last_error = exc
                    line = _overwrite(
                        line, f'retrying (attempt {retry + 1}): {exc.message}')
                else:
                    break
            else:
                raise last_error
            results.append(result)
            sleep(interval)
            line = _overwrite(line, '')
        return mean(results), (0.0 if len(results) == 1 else stdev(results))

    @classmethod
    def _test(cls, func, *required: str, **kwargs) -> tuple[float, float]:
        resolved = []
        for name in required:
            value = kwargs.pop(name, None)
            if not value:
                raise MultimeterUsageError(
                    message=f'the mandatory field "{name}" is missing')
            resolved.append(value)
        return cls._measure(func, *resolved, **kwargs)

    @classmethod
    def _raise_for_status(cls, response) -> None:
        if 400 <= response.status_code <= 499:
            raise MultimeterClientError(status_int=response.status_code,
                                        message=response.reason,
                                        content=response.text)
        if 500 <= response.status_code <= 599:
            raise MultimeterServerError(status_int=response.status_code,
                                        message=response.reason,
                                        content=response.text)


class LatencyTester(BaseTester):
    """Measure the time to open (and tear down) a TCP connection."""

    @classmethod
    def test_latency(cls, **kwargs) -> tuple[float, float]:
        return cls._test(cls._test_latency, 'host', 'port', **kwargs)

    @classmethod
    def _test_latency(cls, host: str, port: int,
                      timeout: float | None = None, **kwargs) -> float:
        begin = perf_counter()
        try:
            with socket.create_connection((host, port),
                                          timeout=timeout) as sock:
                sock.shutdown(socket.SHUT_RD)
        except OSError as exc:
            raise MultimeterServerError(message=str(exc)) from exc
        return perf_counter() - begin


class DownloadTester(BaseTester):
    """Measure the time to download the body of a URL."""

    @classmethod
    def test_download(cls, **kwargs) -> tuple[float, float]:
        return cls._test(cls._test_download, 'url', **kwargs)

    @classmethod
    def _test_download(cls, url: str, headers: dict | None = None,
                       timeout: float | None = None, **kwargs) -> float:
        units = kwargs.get('units')

        aws_config = kwargs.get('aws_config')
        if aws_config:
            headers = S3AuthHeaders(access=aws_config.get('access'),
                                    secret=aws_config.get('secret'),
                                    region=aws_config.get('region'),
                                    url=url, method='get', headers=headers,
                                    payload=b'').to_dict()

        with io.BytesIO() as stream:
            begin = perf_counter()
            response = requests.get(url, stream=True, headers=headers,
                                    timeout=timeout)
            cls._raise_for_status(response)
            total_length = response.headers.get('content-length')
            if total_length is None:
                stream.write(response.content)
            else:
                data_length = 0
                for chunk in response.iter_content(CHUNK_SIZE):
                    data_length += len(chunk)
                    stream.write(chunk)
                    if DEBUG:
                        _write_progress(begin, data_length,
                                        int(total_length), units)
        return perf_counter() - begin


class UploadTester(BaseTester):
    """Measure the time to upload a payload of a given size."""

    @classmethod
    def test_upload(cls, **kwargs) -> tuple[float, float]:
        return cls._test(cls._test_upload, 'url', 'size', **kwargs)

    @classmethod
    def _test_upload(cls, url: str, size: int, headers: dict | None = None,
                     timeout: float | None = None, **kwargs) -> float:
        method = kwargs.get('method')
        send = getattr(requests, method)

        headers = dict(headers) if headers else {}
        headers['content-length'] = str(size)

        aws_config = kwargs.get('aws_config')
        if aws_config:
            headers = S3AuthHeaders(access=aws_config.get('access'),
                                    secret=aws_config.get('secret'),
                                    region=aws_config.get('region'),
                                    url=url, method=method, headers=headers,
                                    payload=bytes(size)).to_dict()

        # Start timing at the request, not before: signing hashes the whole
        # payload, and that shouldn't count as transfer time.
        begin = perf_counter()
        response = send(url, headers=headers, timeout=timeout,
                        data=Streamable(total_length=size,
                                        chunk_size=CHUNK_SIZE))
        cls._raise_for_status(response)
        return perf_counter() - begin


class Multimeter(LatencyTester, DownloadTester, UploadTester):
    """All three test types on one class."""


def parse_args():
    parser = ArgumentParser(
        description='Measure upload/download speed and latency to a host.')

    parser.add_argument('--type', dest='test_type',
                        help='test type: latency, download or upload')
    parser.add_argument('--version', action='store_true',
                        help='print version information and exit')
    parser.add_argument('--debug', action='store_true',
                        help='print progress information while running')
    parser.add_argument('--interval', default=0.1, type=float,
                        help='seconds to wait between runs (default: 0.1)')
    parser.add_argument('--runs', default=2, type=int,
                        help='number of runs to average (default: 2)')
    parser.add_argument('--timeout', default=10, type=float,
                        help='per-request timeout in seconds (default: 10)')
    parser.add_argument('--bytes', dest='units', action='store_const',
                        const=('byte', 1), default=('bit', 8),
                        help='report speeds in bytes instead of bits')
    parser.add_argument('--headers', default='{}',
                        help='request headers as JSON, e.g. \'{"k": "v"}\'')
    parser.add_argument('--host', help='target host (required for latency)')
    parser.add_argument('--port', type=int,
                        help='target port (required for latency)')
    parser.add_argument('--url',
                        help='target URL (required for download/upload)')
    parser.add_argument('--size', type=int,
                        help='payload size in bytes (required for upload)')
    parser.add_argument('--post', dest='method', action='store_const',
                        const='post', default='put',
                        help='use POST instead of PUT for upload')
    parser.add_argument('--aws-config', dest='aws_config',
                        help='S3 SigV4 credentials as JSON, e.g. '
                             '\'{"access": "...", "secret": "...", '
                             '"region": "..."}\'')

    return parser.parse_args()


def version() -> None:
    prog = os.path.basename(sys.argv[0])
    python_version = ' '.join(sys.version.split())
    print(f'{prog} {__version__}')
    print(f'Python {python_version}')
    sys.exit(0)


def shell() -> None:
    global DEBUG
    args = parse_args()

    if args.version:
        version()
    DEBUG = args.debug

    try:
        if args.test_type not in TEST_TYPES:
            raise MultimeterUsageError(
                message=f'--type must be one of: {", ".join(TEST_TYPES)}')
        args.headers = loads(args.headers) if args.headers else {}
        if args.aws_config:
            args.aws_config = loads(args.aws_config)
        measure = getattr(Multimeter, f'test_{args.test_type}')
        ave, std = measure(**vars(args))
        print(f'{ave:.3f} {std:.3f}')
    except MultimeterException as exc:
        sys.stderr.write(str(exc))
        sys.exit(1)


if __name__ == '__main__':
    shell()
