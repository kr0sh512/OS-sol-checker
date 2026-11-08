"""Build-time filesystem regression; never compiles or executes C on the host."""
import importlib.util
from pathlib import Path


def test_library_alias_under_symlinked_parent_resolves_inside_rootfs(tmp_path):
    module_path = Path(__file__).resolve().parents[2] / "sandbox/build_rootfs.py"
    spec = importlib.util.spec_from_file_location("checker_rootfs_builder", module_path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = tmp_path / "source"
    libraries = source / "usr/lib"
    libraries.mkdir(parents=True)
    (source / "lib").symlink_to("usr/lib")
    library = libraries / "libexample.so.1.2"
    library.write_bytes(b"trusted fixture, not executable")
    (libraries / "libexample.so.1").symlink_to(library.name)
    jail = tmp_path / "jail"
    jail_source = jail / str(source).lstrip("/")
    (jail_source / "usr/lib").mkdir(parents=True)
    (jail_source / "lib").symlink_to("usr/lib")

    builder.copy_file(source / "lib/libexample.so.1", jail)

    copied = jail_source / "lib/libexample.so.1"
    assert copied.is_symlink()
    assert copied.read_bytes() == library.read_bytes()
    assert copied.resolve().is_relative_to(jail)
