"""yask — run with ``python -m yask`` or the ``yask`` entry point."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
