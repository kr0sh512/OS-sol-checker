from __future__ import annotations

import os
import sys
from pathlib import Path

SECRETS = {
    "web": ("TRANSIENT_DATABASE_URL", "CSRF_SECRET"),
    "worker": ("TRANSIENT_DATABASE_URL", "ARCHIVE_DATABASE_URL", "RUNNER_TOKEN"),
    "catalog": ("TRANSIENT_DATABASE_URL",),
    "runner": ("RUNNER_TOKEN",),
    "migrate": ("TRANSIENT_MIGRATION_DATABASE_URL", "ARCHIVE_MIGRATION_DATABASE_URL"),
}


def main() -> None:
    role = os.environ["CHECKER_SERVICE"]
    for name in SECRETS[role]:
        filename = os.environ.pop(name + "_FILE", None)
        if filename:
            path = Path(filename)
            if not path.is_file() or path.stat().st_size > 16384:
                raise SystemExit("Missing or oversized role secret")
            value = path.read_text().strip()
            if not value or "\x00" in value or "\n" in value:
                raise SystemExit("Invalid role secret")
            os.environ[name] = value
        if not os.environ.get(name):
            raise SystemExit("Required role secret is absent")
    if len(sys.argv) < 2:
        raise SystemExit("Service command is required")
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    main()
