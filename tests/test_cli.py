import pytest

import netperftest.netperftest as npt
from netperftest.netperftest import MultimeterUsageError, __version__


def test_missing_required_field_raises_usage_error():
    with pytest.raises(MultimeterUsageError):
        # host is required for latency and is absent here.
        npt.Multimeter.test_latency(port=80, runs=1, interval=0)


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        npt.version()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_shell_reports_result(monkeypatch, capsys):
    monkeypatch.setattr(npt.Multimeter, 'test_latency',
                        lambda **kwargs: (0.123, 0.045))
    monkeypatch.setattr(npt.sys, 'argv',
                        ['netperftest', '--type', 'latency', '--host', 'h',
                         '--port', '80', '--runs', '1'])
    npt.shell()
    assert '0.123 0.045' in capsys.readouterr().out


def test_shell_invalid_type_exits_nonzero(monkeypatch):
    monkeypatch.setattr(npt.sys, 'argv', ['netperftest', '--type', 'bogus'])
    with pytest.raises(SystemExit) as exc:
        npt.shell()
    assert exc.value.code == 1
