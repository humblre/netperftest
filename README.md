# netperftest

A small CLI for measuring latency, download, and upload performance against a
single destination.

Everything runs over one connection with no parallelism, so a request has the
bandwidth to itself while it runs. Each test repeats a few times (`--runs`) and
prints the mean and standard deviation, which makes a flaky link easy to spot.

It measures three things:

- latency: time to open and close a TCP connection to `host:port`
- download: time to fetch the body of a URL
- upload: time to send a payload of a given size, with PUT or POST

Download and upload requests can be signed for S3 with AWS Signature Version 4.
Speeds are reported in bits by default, or bytes with `--bytes`.

## Install

```bash
pip install .
```

For development (tests and the linter):

```bash
pip install -e '.[dev]'
```

Needs Python 3.9+. The only runtime dependency is `requests`.

## Usage

```bash
netperftest --type latency --host example.com --port 443 --runs 10
```

Or without installing:

```bash
python -m netperftest --type latency --host example.com --port 443
```

The result is one line on stdout, `<mean seconds> <stddev>`. Progress is
written to stderr, so piping the result stays clean.

A few more examples:

```bash
# download throughput, with a live progress bar
netperftest --type download --url https://example.com/big.bin --debug

# upload 10 MiB with a PUT
netperftest --type upload --url https://example.com/upload --size 10485760

# signed S3 upload
netperftest --type upload --url https://bucket.s3.amazonaws.com/key \
  --size 1048576 \
  --aws-config '{"access": "...", "secret": "...", "region": "..."}'
```

Run `netperftest --help` for the rest of the options.

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check .
```
