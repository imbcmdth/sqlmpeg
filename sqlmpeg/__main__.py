"""Allow `python -m sqlmpeg ...` as an install-free entry point."""

import sys

from sqlmpeg.cli import main

if __name__ == "__main__":
    sys.exit(main())
