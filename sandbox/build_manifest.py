import json
import sys
from pathlib import Path

from os_sol_checker.config import Settings
from os_sol_checker.runner.manifest import NSJAIL_COMMIT, fingerprint, manifest_entries

entries = manifest_entries(Settings())
Path(sys.argv[1]).write_text(json.dumps({"version": 1, "nsjail_commit": NSJAIL_COMMIT,
                                       "fingerprint": fingerprint(entries), "entries": entries},
                                      sort_keys=True, separators=(",", ":")))
