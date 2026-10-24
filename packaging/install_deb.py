#!/usr/bin/python3
"""Install a .deb by extracting it. Does not use apt."""

from __future__ import annotations

import gzip
import io
import os
import sys
import tarfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("用法：sudo python3 install_deb.py lftcli_0.1.0_all.deb\n")
        raise SystemExit(2)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.stderr.write("错误：请用 sudo 运行，这样才能写入 /usr。\n")
        raise SystemExit(1)
    package = Path(sys.argv[1])
    if not package.is_file():
        sys.stderr.write(f"错误：找不到 {package}\n")
        raise SystemExit(1)
    install_deb(package, Path("/"))
    sys.stdout.write("已安装。打开新终端后执行 lftcli。\n")


def install_deb(package: Path, root: Path) -> None:
    members = read_ar(package.read_bytes())
    payload = members.get("data.tar.gz")
    if payload is None:
        raise SystemExit("错误：这个文件不是可安装的 deb 包。")
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
        with tarfile.open(fileobj=compressed, mode="r:") as archive:
            for member in archive:
                target = safe_target(root, member.name)
                if target is None:
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isreg():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.write_bytes(source.read())
                if target.name == "lftcli" and target.parent.name == "bin":
                    target.chmod(0o755)
                else:
                    target.chmod(0o644)


def safe_target(root: Path, name: str) -> Path | None:
    relative = name.lstrip("./")
    if not relative.startswith("usr/") or ".." in Path(relative).parts:
        return None
    return root.joinpath(*Path(relative).parts)


def read_ar(blob: bytes) -> dict[str, bytes]:
    if not blob.startswith(b"!<arch>\n"):
        raise SystemExit("错误：这个文件不是 deb 包。")
    offset = 8
    members: dict[str, bytes] = {}
    while offset + 60 <= len(blob):
        header = blob[offset : offset + 60]
        name = header[0:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        offset += 60
        members[name] = blob[offset : offset + size]
        offset += size + (size % 2)
    return members


if __name__ == "__main__":
    main()
