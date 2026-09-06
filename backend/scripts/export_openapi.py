"""Export the OpenAPI schema to a file.

The committed schema is the contract the frontend client is generated from,
so CI can diff it and fail when the two drift apart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import create_app


def main(destination: Path) -> None:
    schema = create_app().openapi()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys makes the output stable so the diff reflects real changes only.
    destination.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {destination}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    main(target)
