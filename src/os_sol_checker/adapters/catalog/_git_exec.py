"""Private exec helper: set hard limits without unsafe preexec_fn in a thread."""
import os
import resource
import sys


def main() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execve(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    main()
