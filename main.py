"""Tracyy launcher.

Kept deliberately tiny. Spawned decoder and analysis workers re-import this
file as ``__mp_main__``; anything imported at module scope here would be paid
for by every worker process, so the GUI stack is only imported inside
:func:`main`.
"""

from __future__ import annotations

import multiprocessing


def main() -> None:
    from tracyy.app import main as run_tracyy

    run_tracyy()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
