from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description="Create new Compose secrets, never overwrite existing credentials")
    parser.add_argument("--directory", type=Path, default=Path(__file__).parent / "secrets")
    parser.add_argument("--public-origin", required=True)
    args = parser.parse_args()
    origin = urlsplit(args.public_origin)
    if (origin.scheme != "https" or not origin.netloc or origin.username or origin.password
            or origin.path not in {"", "/"} or origin.query or origin.fragment):
        parser.error("--public-origin must be an HTTPS origin")
    if os.geteuid() != 0:
        parser.error("run as root: file-backed Compose secrets need the actual service UID ownership")
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(directory.iterdir()):
        parser.error("secret directory must be empty; rotation requires an explicit database procedure")
    directory.chmod(0o700)
    values: dict[str, tuple[str, int]] = {}
    passwords = {name: secrets.token_urlsafe(36) for name in
                 ("transient_owner", "archive_owner", "web", "worker", "catalog", "archive_worker")}
    for name, password in passwords.items():
        values[name + "_password"] = (password, 999)
    for name, user in (("web", "checker_web"), ("worker", "checker_worker"), ("catalog", "checker_catalog")):
        values[name + "_database_url"] = (
            f"postgresql://{user}:{passwords[name]}@postgres-transient:5432/checker_transient", 10001)
    values["archive_database_url"] = (
        f"postgresql://checker_archive_runtime:{passwords['archive_worker']}@postgres-archive:5432/checker_archive", 10001)
    values["transient_migration_database_url"] = (
        f"postgresql://checker_owner:{passwords['transient_owner']}@postgres-transient:5432/checker_transient", 10001)
    values["archive_migration_database_url"] = (
        f"postgresql://checker_owner:{passwords['archive_owner']}@postgres-archive:5432/checker_archive", 10001)
    values["csrf_secret"] = (secrets.token_urlsafe(48), 10001)
    values["runner_token"] = (secrets.token_urlsafe(48), 10001)
    for name, (value, uid) in values.items():
        path = directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o400)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")
        os.chown(path, uid, uid)
    print("Created role-scoped secret files. Set PUBLIC_ORIGIN=" + args.public_origin.rstrip("/"))


if __name__ == "__main__":
    main()
