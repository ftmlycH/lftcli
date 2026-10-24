"""终端输出。不访问网络。"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata


def print_post_list(posts) -> None:
    if not posts:
        print("（没有结果）")
        return
    for post in posts:
        title = _single_line(getattr(post, "title", "") or "无标题")
        author = _single_line(getattr(post, "author", "") or "未知")
        hot = getattr(post, "hot", 0)
        print(f"[{post.post_id}] {title} · {author} · 热度 {hot}")


def print_blog_list(blogs) -> None:
    if not blogs:
        print("（没有结果）")
        return
    for blog in blogs:
        name = _single_line(getattr(blog, "name", "") or "")
        nickname = _single_line(getattr(blog, "nickname", "") or "未知")
        print(f"[{name}] {nickname}")


def print_collection_list(collections) -> None:
    if not collections:
        print("（没有结果）")
        return
    for item in collections:
        name = _single_line(item.name or "未命名合集")
        if not item.valid:
            name += "（已失效）"
        author = _single_line(item.author or "未知")
        if item.post_count is None:
            tail = "篇数未知"
        else:
            tail = f"{item.post_count}篇"
        print(f"[{item.collection_id}] {name} · {author} · {tail}")


def print_article(post) -> None:
    title = _single_line(getattr(post, "title", "") or "无标题")
    author = _single_line(getattr(post, "author", "") or "未知")
    text = getattr(post, "text", "") or ""
    columns = _terminal_columns()
    if columns:
        text = "\n".join(piece for line in text.split("\n") for piece in _wrap_line(line, columns))
        rule = "─" * max(8, min(24, columns // 2))
    else:
        rule = "─" * 24
    print(f"标题：{title}")
    print(f"作者：{author}")
    print(rule)
    print(text)


def clear_screen() -> None:
    if not _is_tty(sys.stdout):
        return
    if _ansi_clear_works():
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
        return
    command = "cls" if os.name == "nt" else "clear"
    os.system(command)


def prepare_terminal() -> None:
    if os.name == "nt":
        _enable_windows_vt()
        return
    try:
        import readline  # noqa: F401
    except ImportError:
        return


def print_info(msg: str) -> None:
    print(msg)


def print_error(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)


def _single_line(value: str) -> str:
    return " ".join(str(value).split())


def _terminal_columns() -> int:
    if not _is_tty(sys.stdout):
        return 0
    try:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    except OSError:
        return 80
    return columns if columns >= 8 else 0


def _wrap_line(line: str, columns: int) -> list[str]:
    if columns < 1 or _display_width(line) <= columns:
        return [line]
    rows: list[str] = []
    current: list[str] = []
    width = 0
    for char in line:
        char_width = _char_width(char)
        if current and width + char_width > columns:
            rows.append("".join(current))
            current = []
            width = 0
        current.append(char)
        width += char_width
    if current:
        rows.append("".join(current))
    return rows or [""]


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _char_width(char: str) -> int:
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def _is_tty(stream) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _ansi_clear_works() -> bool:
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    return _enable_windows_vt()


def _enable_windows_vt() -> bool:
    try:
        import ctypes
    except ImportError:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    enable_virtual_terminal = 0x0004
    return bool(kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal))
