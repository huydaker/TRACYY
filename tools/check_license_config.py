from __future__ import annotations

import json

from tracyy.licensing.client import LicenseClient


def main() -> None:
    client = LicenseClient()
    print(
        json.dumps(
            client.config_debug_info(),
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
