#!/usr/bin/env python3
"""
A tiny script that measures latency and upload/download speeds for a specified
destination.

No parallel mechanisms are used to ensure that a single thread has exclusive
use of the bandwidth.
"""
import io
import os
import socket
import sys
from time import sleep, perf_counter
from argparse import ArgumentParser, ArgumentError
from traceback import format_exc
from typing import Optional
from json import loads
from statistics import stdev
import requests
from importlib import import_module

__version__ = '0.0.1'
CHUNK_SIZE = 1 << 13
DEBUG = False


class MultimeterException(Exception):
    """Base Exception"""
    def __init__(self, status_int=None, message='', content=''):
        self.status_int = status_int
        if status_int:
            self.message = '%d %s' % (status_int, message)
        else:
            self.message = '%s' % (message)
        self.content = content

        super().__init__(self.message)

    def __str__(self):
        return '\033[1m\033[31m%s\033[0m\n\n\033[31m%s\033[0m\n' % (
            self.message, self.content)


class MultimeterClientError(MultimeterException):
    """HTTP 4xx Error"""


class MultimeterServerError(MultimeterException):
    """HTTP 5xx Error"""


class Streamable(object):
    """Iterable for Streaming Upload"""
    def __init__(self, total_length, chunk_size=CHUNK_SIZE):
        self.total_length = total_length
        self.data_length = 0
        self.chunk_size = chunk_size

    def __iter__(self):
        while self.data_length < self.total_length:
            self.data_length += self.chunk_size
            if self.data_length > self.total_length:
                self.data_length = self.total_length
                self.chunk_size = self.total_length % self.chunk_size
            if DEBUG:
                ratio = self.data_length * 1e2 / self.total_length
                sys.stderr.write('%3.3f %%\r' % ratio)
            if self.chunk_size:
                yield bytes(self.chunk_size)

    def __len__(self):
        return self.total_length


class AWSAuthHeaders(object):
    """Headers with Authorization (AWS Signature Version 4)"""
    @property
    def algorithm(self):
        return 'AWS4-HMAC-SHA256'

    @property
    def service(self):
        raise NotImplementedError()

    @property
    def aws4_request(self):
        return 'aws4_request'

    def to_dict(self):
        return self._headers

    def __init__(self, access, secret, region, url, method, headers, payload):
        datetime = import_module('datetime')
        hmac = import_module('hmac')
        hashlib = import_module('hashlib')
        urllib_parse = import_module('urllib.parse')

        def sign(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        utc_now = datetime.datetime.utcnow()
        ymdhms = utc_now.strftime('%Y%m%dT%H%M%SZ')
        ymd = utc_now.strftime('%Y%m%d')

        payload_hash = hashlib.sha256(payload).hexdigest()

        credential_scope = '/'.join([
            ymd, region, self.service, self.aws4_request])

        parsed_url = urllib_parse.urlparse(url)
        host = parsed_url.netloc
        query = parsed_url.query
        path = parsed_url.path or '/'

        self._headers = {} if headers is None else headers.copy()
        self._headers['X-Amz-Date'] = ymdhms
        self._headers['X-Amz-Content-Sha256'] = payload_hash
        self._headers['Host'] = host

        canonical_headers = '\n'.join(
            sorted([':'.join([k.lower(), v]) for k, v in self._headers.items()]
                   )) + '\n'
        signed_headers = ';'.join(
            sorted([k.lower() for k in self._headers.keys()]))

        canonical_request = '\n'.join([method.upper(), path, query,
                                       canonical_headers, signed_headers,
                                       payload_hash])

        string_to_sign = '\n'.join([
            self.algorithm, ymdhms, credential_scope,
            hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()])

        signing_key = sign(sign(sign(sign(('AWS4' + secret).encode('utf-8'),
                                          ymd),
                                     region),
                                self.service),
                           self.aws4_request)

        signature = hmac.new(signing_key, string_to_sign.encode('utf-8'),
                             hashlib.sha256).hexdigest()

        self._headers['Authorization'] = (
            self.algorithm + ' ' +
            'Credential=' + access + '/' + credential_scope + ', ' +
            'SignedHeaders=' + signed_headers + ', ' +
            'Signature=' + signature)


class S3AuthHeaders(AWSAuthHeaders):
    """Headers with Authorization for S3"""
    @property
    def service(self):
        return 's3'


class BaseTester(object):
    """Base Class providing these common interfaces"""
    @classmethod
    def __test(cls, func, *args, **kwargs):
        interval = kwargs.pop('interval', 0.1)
        runs = kwargs.pop('runs', 2)
        timeout = kwargs.pop('timeout', 10.0)
        results = []

        s = ''
        for i in range(runs):
            for retry in range(10):
                try:
                    if not retry:
                        s = '%%-%ds\r' % len(s) % (
                            '> %s in progress for %d times' % (
                                func.__name__, i + 1))
                        sys.stdout.write(s)
                    result = func(*args, timeout=timeout, **kwargs)
                except MultimeterException as e:
                    s = '%%-%ds\r' % len(s) % (
                        'Retryin for the %d times, due to %s' % (retry + 1, e))
                    sys.stdout.write(s)
                else:
                    break
            results.append(result)
            sleep(interval)
            sys.stdout.write('%%-%ds\r' % len(s) % '')

        return (sum(results) / len(results),
                0 if len(results) == 1 else stdev(results))

    @classmethod
    def _test(cls, func, *args, **kwargs) -> Optional[float]:
        _args = []
        for arg in args:
            value = kwargs.pop(arg)
            if not value:
                raise ArgumentError(
                    value, message='The mandatory field "%s" missing' % arg)
            _args.append(value)
        return cls.__test(func, *_args, **kwargs)

    @classmethod
    def _raise_for_status(cls, response):
        if 400 <= response.status_code <= 499:
            raise MultimeterClientError(status_int=response.status_code,
                                        message=response.reason,
                                        content=response.text)
        if 500 <= response.status_code <= 599:
            raise MultimeterServerError(status_int=response.status_code,
                                        message=response.reason,
                                        content=response.text)


class LatencyTester(BaseTester):
    """Latency Test Impl"""
    @classmethod
    def test_latency(cls, **kwargs):
        return cls._test(cls._test_latency, 'host', 'port', **kwargs)

    @classmethod
    def _test_latency(cls, host: str, port: int,
                      timeout: float = None, **kwargs):
        begin = perf_counter()

        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.shutdown(socket.SHUT_RD)
        except (socket.timeout, Exception) as exc:
            raise MultimeterServerError(message=str(exc)) from exc

        return float(perf_counter() - begin)


class DownloadTester(BaseTester):
    """Download Test Impl"""
    @classmethod
    def test_download(cls, **kwargs):
        return cls._test(cls._test_download, 'url', **kwargs)

    @classmethod
    def _test_download(cls, url: str,
                       headers: dict = None, timeout: float = None, **kwargs):
        units = kwargs.get('units')

        aws_config = kwargs.get('aws_config')
        if aws_config:
            headers = S3AuthHeaders(access=aws_config.get('access'),
                                    secret=aws_config.get('secret'),
                                    region=aws_config.get('region'),
                                    url=url,
                                    method='get',
                                    headers=headers,
                                    payload=b'').to_dict()

        with io.BytesIO() as stream:
            begin = perf_counter()
            response = requests.get(url, stream=True, headers=headers,
                                    timeout=timeout)
            cls._raise_for_status(response)
            total_length = response.headers.get('content-length')
            data_length = 0
            if total_length is None:
                stream.write(response.content)
            else:
                for chunk in response.iter_content(CHUNK_SIZE):
                    data_length += len(chunk)
                    stream.write(chunk)
                    if DEBUG:
                        elapsed = perf_counter() - begin
                        done = int(40 * data_length / int(total_length))
                        fmstr = '[%s%s] %3.3f Mega%ss/s\r' % (
                            '=' * done, ' ' * (40 - done),
                            data_length * units[1] / elapsed / 1e6, units[0])
                        sys.stdout.write(fmstr)
        return perf_counter() - begin


class UploadTester(BaseTester):
    """Upload Test Impl"""
    @classmethod
    def test_upload(cls, **kwargs) -> list:
        return cls._test(cls._test_upload,
                         'url', 'size', **kwargs)

    @classmethod
    def _test_upload(cls, url: str, size: int, headers: dict = None,
                     timeout: float = None, **kwargs):
        method = kwargs.get('method')
        put_or_post = getattr(requests, method)
        begin = perf_counter()

        headers = {} if headers is None else headers
        headers['content-length'] = str(size)

        aws_config = kwargs.get('aws_config')
        if aws_config:
            headers = S3AuthHeaders(access=aws_config.get('access'),
                                    secret=aws_config.get('secret'),
                                    region=aws_config.get('region'),
                                    url=url,
                                    method=method,
                                    headers=headers,
                                    payload=bytes(size)).to_dict()

        response = put_or_post(url, headers=headers, timeout=timeout,
                               data=Streamable(total_length=size,
                                               chunk_size=CHUNK_SIZE))
        cls._raise_for_status(response)
        return perf_counter() - begin


class Multimeter(LatencyTester, DownloadTester, UploadTester):
    """Handler Interface"""


def parse_args():
    description = ('CLI for measuring upload, download speeds, and latency. '
                   'The type latency requires at least the args host and port,'
                   ' download requires at least the arg url,'
                   ' and upload requires at least url, size and headers.')
    parser = ArgumentParser(description=description)

    # Required
    parser.add_argument('--type', dest='test_type', required=True,
                        help='Test type (latency, upload or download)')
    # Debugging
    parser.add_argument('--version', action='store_true',
                        help='Program version')
    parser.add_argument('--debug', action='store_true',
                        help='Make the operation more talkative')
    # Common
    parser.add_argument('--interval', default=0.1, type=float,
                        help='Delay seconds between requests')
    parser.add_argument('--runs', default=2, type=int,
                        help='Number of times to attempt a request')
    parser.add_argument('--timeout', default=10, type=float,
                        help='Request timeout')
    parser.add_argument('--bytes', dest='units', action='store_const',
                        const=('byte', 1), default=('bit', 8),
                        help='Display values in bytes instead of bits')
    parser.add_argument('--headers', type=str, default='{}',
                        help='Headers, which must be of the form {"k":"v",..}')
    # For latency
    parser.add_argument('--host',
                        help='Target host, which is required for "latency"')
    parser.add_argument('--port', type=int,
                        help='Target port, which is required for "latency"')
    # For upload and download
    parser.add_argument('--url',
                        help='Target URL, which is required for "up/download"')
    # For upload
    parser.add_argument('--size', type=int,
                        help='Size to upload, which is required for "upload"')
    parser.add_argument('--post', dest='method', action='store_const',
                        const='post', default='put',
                        help='If it is set, use POST instead of PUT on upload')
    # For AWS
    parser.add_argument('--aws-config', dest='aws_config', type=str,
                        help='Material to make a request for S3, which must be'
                        ' of the form {"access": s, "secret": s, "region": s}')

    return parser.parse_args()


def version():
    print('%s %s' % (os.path.basename(sys.argv[0]), __version__))
    print('Python %s' % sys.version.replace('\n', ''))
    sys.exit(0)


def shell():
    args = parse_args()

    if args.version:
        version()
    if args.debug:
        global DEBUG
        DEBUG = True

    try:
        if args.test_type not in ('latency', 'upload', 'download'):
            raise ArgumentError(args.test_type,
                                message='The argument "type" must be either '
                                'latency, upload or download.')
        test_type = '_'.join(['test', args.test_type])
        if args.headers:
            args.headers = loads(args.headers)
        if args.aws_config:
            args.aws_config = loads(args.aws_config)
        ave, std = getattr(Multimeter, test_type)(**vars(args))
        sys.stdout.write('%.3f %.3f' % (ave, std))
    except MultimeterException:
        sys.stderr.write(format_exc())
        sys.exit(1)


if __name__ == '__main__':
    shell()
