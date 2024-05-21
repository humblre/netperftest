import sys

from netperftest.netperftest import *  # noqa: F403 F401
from netperftest.netperftest import shell


if __name__ == 'netperftest':
    sys.exit(shell())
