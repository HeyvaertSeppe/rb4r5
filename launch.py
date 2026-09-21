#!/usr/bin/env python3
"""rb4r5 launcher - run the XDJ-RX3 rekordbox player on a Raspberry Pi 5.

    sudo python3 launch.py            # set up what is missing, then run
    sudo python3 launch.py doctor      # check every subsystem
    sudo python3 launch.py --help      # every command

Needs nothing but python3 from a stock Raspberry Pi OS image.
"""
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("rb4r5 needs Python 3.9 or newer (Raspberry Pi OS Bookworm has 3.11)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rb4r5.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
