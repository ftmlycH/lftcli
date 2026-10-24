"""Build an architecture-independent .deb without dpkg.

Usage:
    python packaging/build_deb.py
    python packaging/build_deb.py --root debian/lftcli
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
PACKAGE = "lftcli"
MODULES = ("main.py", "client.py", "parser.py", "output.py", "config.py")
SHARE = Path("usr/share/lftcli")
BIN = Path("usr/bin/lftcli")
DOC = Path("usr/share/doc/lftcli")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the lftcli .deb package")
    parser.add_argument("--root", type=Path, help="stage the package tree here instead of writing a .deb")
    parser.add_argument("--output", type=Path, help="path of the .deb file")
    args = parser.parse_args()
    if args.root:
        stage_tree(args.root)
        print(args.root)
        return
    output = args.output or ROOT / "dist" / f"{PACKAGE}_{VERSION}_all.deb"
    build_deb(output)
    print(output)


def stage_tree(root: Path) -> None:
    if root.exists():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    files = installed_files()
    for arcname, data, mode in files:
        path = root / arcname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)


def build_deb(output: Path) -> None:
    files = installed_files()
    data = gzip_tar(files, directories=True)
    control = gzip_tar(
        [
            ("control", control_text(files).encode("utf-8"), 0o644),
            ("md5sums", md5sums(files).encode("utf-8"), 0o644),
        ],
        directories=False,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(ar_archive([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control),
        ("data.tar.gz", data),
    ]))
    installer = ROOT / "packaging" / "install_deb.py"
    if installer.is_file() and output.parent != installer.parent:
        target = output.parent / "install_deb.py"
        target.write_bytes(installer.read_bytes().replace(b"\r\n", b"\n"))


def installed_files() -> list[tuple[str, bytes, int]]:
    files: list[tuple[str, bytes, int]] = []
    wrapper = (ROOT / "packaging" / "lftcli").read_bytes().replace(b"\r\n", b"\n")
    files.append((BIN.as_posix(), wrapper, 0o755))
    for name in MODULES:
        files.append(((SHARE / name).as_posix(), (ROOT / name).read_bytes().replace(b"\r\n", b"\n"), 0o644))
    files.append(((DOC / "README.md").as_posix(), (ROOT / "README.md").read_bytes().replace(b"\r\n", b"\n"), 0o644))
    files.append(((DOC / "copyright").as_posix(), (ROOT / "debian" / "copyright").read_bytes().replace(b"\r\n", b"\n"), 0o644))
    return files


def control_text(files: list[tuple[str, bytes, int]]) -> str:
    text = (ROOT / "debian" / "control").read_text(encoding="utf-8")
    binary: list[str] = []
    started = False
    for line in text.splitlines():
        if line.startswith("Package:"):
            started = True
        if started:
            binary.append(line.rstrip("\r"))
    if not binary:
        raise RuntimeError("debian/control has no binary package")
    installed = max(1, (sum(len(data) for _name, data, _mode in files) + 1023) // 1024)
    sized = []
    for line in binary:
        sized.append(line)
        if line.startswith("Architecture:"):
            sized.append(f"Installed-Size: {installed}")
    return "\n".join(sized).rstrip() + "\n"


def md5sums(files: list[tuple[str, bytes, int]]) -> str:
    lines = []
    for arcname, data, _mode in files:
        digest = hashlib.md5(data).hexdigest()
        lines.append(f"{digest}  {arcname}")
    return "\n".join(lines) + "\n"


def gzip_tar(files: list[tuple[str, bytes, int]], directories: bool) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            stamp = int(time.time())
            if directories:
                for directory in parent_directories(files):
                    info = tar_info(f"./{directory}", 0, 0o755, stamp)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
            for arcname, data, mode in files:
                info = tar_info(f"./{arcname}", len(data), mode, stamp)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def parent_directories(files: list[tuple[str, bytes, int]]) -> list[str]:
    found: set[str] = set()
    for arcname, _data, _mode in files:
        parent = Path(arcname).parent
        while str(parent) not in {"", "."}:
            found.add(parent.as_posix())
            parent = parent.parent
    return sorted(found)


def tar_info(name: str, size: int, mode: int, stamp: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.mtime = stamp
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def ar_archive(members: list[tuple[str, bytes]]) -> bytes:
    chunks = [b"!<arch>\n"]
    for name, data in members:
        chunks.append(ar_header(name, len(data)))
        chunks.append(data)
        if len(data) % 2:
            chunks.append(b"\n")
    return b"".join(chunks)


def ar_header(name: str, size: int) -> bytes:
    if len(name) > 16:
        raise ValueError(f"ar name too long: {name}")
    header = b"".join(
        [
            f"{name}/".encode("ascii").ljust(16),
            str(int(time.time())).encode("ascii").ljust(12),
            b"0".ljust(6),
            b"0".ljust(6),
            b"100644".ljust(8),
            str(size).encode("ascii").ljust(10),
            b"`\n",
        ]
    )
    if len(header) != 60:
        raise RuntimeError(f"bad ar header length: {len(header)}")
    return header


if __name__ == "__main__":
    main()
