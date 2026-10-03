"""``python -m tracyy``."""

from __future__ import annotations

import multiprocessing

from tracyy.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
