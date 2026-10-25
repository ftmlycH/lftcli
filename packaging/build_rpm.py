"""Build a noarch .rpm without rpmbuild.

Install on Fedora / RHEL:
    sudo rpm -i --nosignature dist/lftcli-0.1.0-1.noarch.rpm
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import struct
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
RELEASE = "1"
PACKAGE = "lftcli"

# RPM header types
NULL = 0
CHAR = 1
INT8 = 2
INT16 = 3
INT32 = 4
STRING = 6
BIN = 7
STRING_ARRAY = 8
I18NSTRING = 9


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the lftcli .rpm package")
    parser.add_argument("--output", type=Path, help="path of the .rpm file")
    args = parser.parse_args()
    output = args.output or ROOT / "dist" / f"{PACKAGE}-{VERSION}-{RELEASE}.noarch.rpm"
    build_rpm(output)
    print(output)


def build_rpm(output: Path) -> None:
    files = installed_files()
    payload, digests = cpio_gz(files)
    header = make_header(files, digests)
    signature = make_signature(header, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(lead() + pad(signature, 8) + header + payload)


def installed_files() -> list[tuple[str, bytes, int]]:
    script = ROOT / "packaging" / "build_deb.py"
    spec = importlib.util.spec_from_file_location("lftcli_build_deb", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.installed_files()


def lead() -> bytes:
    name = f"{PACKAGE}-{VERSION}-{RELEASE}".encode("ascii")[:65]
    return struct.pack(
        "!4sBBHH66sHH16s",
        b"\xed\xab\xee\xdb",
        3,
        0,
        0,
        1,
        name.ljust(66, b"\0"),
        1,
        5,
        b"\0" * 16,
    )


def make_signature(header: bytes, payload: bytes) -> bytes:
    blob = header + payload
    return header_blob(
        [
            (1000, INT32, [len(blob)]),
            (1004, BIN, hashlib.md5(blob).digest()),
        ],
        region=62,
    )


def make_header(files: list[tuple[str, bytes, int]], digests: list[str]) -> bytes:
    basenames = [Path(name).name for name, _data, _mode in files]
    dirnames = []
    dirindexes = []
    dirs: dict[str, int] = {}
    for name, _data, _mode in files:
        directory = "/" + Path(name).parent.as_posix().strip("/") + "/"
        if directory == "//":
            directory = "/"
        if directory not in dirs:
            dirs[directory] = len(dirnames)
            dirnames.append(directory)
        dirindexes.append(dirs[directory])
    sizes = [len(data) for _name, data, _mode in files]
    modes = [0o100000 | mode for _name, _data, mode in files]
    mtime = int(time.time())
    return header_blob(
        [
            (1000, STRING, PACKAGE),
            (1001, STRING, VERSION),
            (1002, STRING, RELEASE),
            (1004, I18NSTRING, "Read LOFTER novels in the terminal"),
            (1005, I18NSTRING, "Interactive LOFTER terminal client for text posts."),
            (1009, INT32, [sum(sizes)]),
            (1014, STRING, "LicenseRef-unspecified"),
            (1016, STRING, "Applications/Text"),
            (1021, STRING, "noarch"),
            (1022, STRING, "linux"),
            (1028, INT32, sizes),
            (1030, INT16, modes),
            (1034, INT32, [mtime] * len(files)),
            (1035, STRING_ARRAY, digests),
            (1039, INT32, [0] * len(files)),
            (1040, STRING_ARRAY, ["root"] * len(files)),
            (1041, STRING_ARRAY, ["root"] * len(files)),
            (1048, STRING_ARRAY, ["python3"]),
            (1049, INT32, [0]),
            (1050, STRING_ARRAY, [""]),
            (1095, INT32, [0xFFFFFFFF] * len(files)),
            (1096, INT16, [1] * len(files)),
            (1097, INT32, list(range(1, len(files) + 1))),
            (1098, STRING_ARRAY, [""] * len(files)),
            (1116, STRING_ARRAY, dirnames),
            (1117, STRING_ARRAY, basenames),
            (1118, INT32, dirindexes),
            (1124, STRING, "cpio"),
            (1125, STRING, "gzip"),
            (1126, STRING, "9"),
        ],
        region=63,
    )


def header_blob(entries: list[tuple[int, int, object]], region: int) -> bytes:
    store = bytearray()
    index: list[tuple[int, int, int, int]] = []

    def align(boundary: int) -> None:
        while len(store) % boundary:
            store.append(0)

    def put_string(text: str) -> tuple[int, int]:
        offset = len(store)
        store.extend(text.encode("utf-8") + b"\0")
        return offset, 1

    def put_string_array(values: list[str]) -> tuple[int, int]:
        offset = len(store)
        for value in values:
            store.extend(value.encode("utf-8") + b"\0")
        return offset, len(values)

    def put_bin(data: bytes) -> tuple[int, int]:
        offset = len(store)
        store.extend(data)
        return offset, len(data)

    def put_int16(values: list[int]) -> tuple[int, int]:
        align(2)
        offset = len(store)
        for value in values:
            store.extend(struct.pack("!H", value & 0xFFFF))
        return offset, len(values)

    def put_int32(values: list[int]) -> tuple[int, int]:
        align(4)
        offset = len(store)
        for value in values:
            store.extend(struct.pack("!I", value & 0xFFFFFFFF))
        return offset, len(values)

    # Placeholder region record.
    align(8)
    region_off = len(store)
    store.extend(b"\0" * 16)
    index.append((region, BIN, region_off, 16))

    writers = {
        STRING: lambda value: put_string(str(value)),
        I18NSTRING: lambda value: put_string(str(value)),
        STRING_ARRAY: lambda value: put_string_array(list(value)),
        BIN: lambda value: put_bin(bytes(value)),
        INT16: lambda value: put_int16(list(value)),
        INT32: lambda value: put_int32(list(value)),
    }
    for tag, kind, value in sorted(entries, key=lambda item: item[0]):
        offset, count = writers[kind](value)
        index.append((tag, kind, offset, count))

    # Immutable region: points back over the whole index.
    region_data = struct.pack("!iiii", region, BIN, -len(index) * 16, 16)
    store[region_off : region_off + 16] = region_data

    index_data = b"".join(struct.pack("!IIII", tag, kind, offset, count) for tag, kind, offset, count in index)
    magic = b"\x8e\xad\xe8\x01" + struct.pack("!III", 0, len(index), len(store))
    return magic + index_data + bytes(store)


def pad(data: bytes, boundary: int) -> bytes:
    need = (boundary - len(data) % boundary) % boundary
    return data + (b"\0" * need)


def cpio_gz(files: list[tuple[str, bytes, int]]) -> tuple[bytes, list[str]]:
    digests: list[str] = []
    out = io.BytesIO()
    inode = 1
    for arcname, data, mode in files:
        digests.append(hashlib.md5(data).digest().hex())
        write_cpio(out, "./" + arcname, data, 0o100000 | mode, inode)
        inode += 1
    write_cpio(out, "TRAILER!!!", b"", 0, 0)
    return gzip.compress(out.getvalue(), mtime=0), digests


def write_cpio(out: io.BytesIO, name: str, data: bytes, mode: int, inode: int) -> None:
    name_b = name.encode("utf-8") + b"\0"
    header = "".join(
        [
            "070701",
            f"{inode:08x}",
            f"{mode:08x}",
            "00000000",
            "00000000",
            "00000001",
            f"{len(data):08x}",
            f"{int(time.time()):08x}",
            "00000000",
            "00000000",
            "00000000",
            f"{len(name_b):08x}",
            "00000000",
        ]
    ).encode("ascii")
    out.write(header)
    out.write(name_b)
    out.write(b"\0" * ((4 - (len(header) + len(name_b)) % 4) % 4))
    out.write(data)
    out.write(b"\0" * ((4 - len(data) % 4) % 4))


if __name__ == "__main__":
    main()
