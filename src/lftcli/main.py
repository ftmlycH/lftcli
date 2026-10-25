"""命令入口。"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys

from . import client, config, output
from .parser import html_to_markdown, html_to_text


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    output.prepare_terminal()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return repl()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        cookie = config.require_cookie() if _needs_cookie(args) else ""
        args.func(args, cookie)
        return 0
    except (config.ConfigError, client.AuthError, client.ApiError) as exc:
        output.print_error(str(exc))
        return 1
    except KeyboardInterrupt:
        output.print_error("已取消。")
        return 130
    except Exception as exc:
        if os.environ.get("LOFTER_DEBUG"):
            raise
        output.print_error(str(exc) or exc.__class__.__name__)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lftcli",
        description="在终端读 LOFTER 文字小说。直接运行 lftcli 进入 lofter> 。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  lofter> tag 原创小说 --limit 5\n"
            "  lofter> random 原创小说 --count 3\n"
            "  lofter> recommend 3\n"
            "  lofter> post 123456\n"
            "  lofter> exit\n"
        ),
    )
    commands = parser.add_subparsers(dest="command")

    cookie_cmd = commands.add_parser("cookie", help="查看或保存 Cookie")
    cookie_cmd.set_defaults(func=_cookie_status)
    cookie_actions = cookie_cmd.add_subparsers(dest="cookie_action")
    cookie_actions.add_parser("status", help="是否已保存").set_defaults(func=_cookie_status)
    cookie_set = cookie_actions.add_parser("set", help="保存 Cookie")
    cookie_set.add_argument("--no-check", action="store_true", help="只保存，不验证登录")
    cookie_set.set_defaults(func=_cookie_set)
    cookie_actions.add_parser("check", help="验证当前 Cookie").set_defaults(func=_cookie_check)
    cookie_actions.add_parser("clear", help="删除已保存的 Cookie").set_defaults(func=_cookie_clear)

    for name in ("clear", "cls"):
        screen = commands.add_parser(name, help="清屏")
        screen.set_defaults(func=_clear_screen)

    collections = commands.add_parser("collections", help="订阅合集；加 ID 列出其中文章")
    collections.add_argument("collection_id", nargs="?", help="合集 ID 或链接")
    _add_page_args(collections, limit=20)
    collections.add_argument(
        "--order",
        choices=("old", "new"),
        default="old",
        help="合集内排序：old 旧到新，new 新到旧",
    )
    collections.set_defaults(func=_collections)

    _command(commands, "mine", "我发布的文字", _paged(client.get_my_posts, _show_posts), 20)
    _command(commands, "following", "关注的人发布的文字", _paged(client.get_following_posts, _show_posts), 20)

    _command(commands, "likes", "我喜欢的文章", _paged(client.get_likes, _show_posts), 20)
    _command(commands, "history", "浏览记录", _paged(client.get_history, _show_posts), 20)

    tag = commands.add_parser("tag", help="按标签列出文章")
    tag.add_argument("tag", help="标签名")
    tag.add_argument("--sort", choices=("hot", "new", "month"), default="hot", help="hot / new / month")
    _add_page_args(tag, limit=20)
    tag.set_defaults(func=_tag)

    random_cmd = commands.add_parser("random", help="从标签随机抽文章")
    random_cmd.add_argument("tag", help="标签名")
    random_cmd.add_argument("--sort", choices=("hot", "new", "month"), default="hot", help="hot / new / month")
    random_cmd.add_argument("--count", type=int, default=1, help="篇数，1–5，默认 1")
    random_cmd.set_defaults(func=_random)

    recommend_cmd = commands.add_parser("recommend", help="随机推荐文章")
    recommend_cmd.add_argument("count", type=int, help="篇数，1–5")
    recommend_cmd.set_defaults(func=_recommend)

    post = commands.add_parser("post", help="读一篇正文")
    post.add_argument("post_id_or_url", help="文章 ID 或链接")
    post.add_argument("--markdown", action="store_true", help="输出 Markdown")
    post.set_defaults(func=_post)
    return parser


def _add_page_args(parser: argparse.ArgumentParser, limit: int) -> None:
    parser.add_argument("--limit", type=int, default=limit, help=f"条数，默认 {limit}")
    parser.add_argument("--offset", type=int, default=0, help="跳过条数")


def _command(commands, name: str, help_text: str, func, limit: int) -> None:
    parser = commands.add_parser(name, help=help_text)
    _add_page_args(parser, limit)
    parser.set_defaults(func=func)


def _paged(fetch, show):
    def run(args, cookie: str) -> None:
        rows = fetch(cookie, limit=args.limit, offset=args.offset)
        if getattr(args, "offset", 0) > 0:
            hint = "（没有更多了）"
        else:
            hint = "（这里暂时没有内容）"
        show(rows, empty_message=hint)
    return run


def repl() -> int:
    output.print_info("help 查看指令，exit 退出。")
    while True:
        try:
            line = input("lofter> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        line = line.strip()
        if not line:
            continue
        if line.lower() in {"exit", "quit", "q"}:
            return 0
        if line.lower() in {"help", "?"}:
            line = "-h"
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            output.print_error(f"无法解析这行指令：{exc}")
            continue
        try:
            main(argv)
        except SystemExit:
            continue
    return 0


def _clear_screen(_args, _cookie: str) -> None:
    output.clear_screen()


def _needs_cookie(args) -> bool:
    if getattr(args, "command", None) in {None, "clear", "cls"}:
        return False
    if args.command != "cookie":
        return True
    return getattr(args, "cookie_action", None) == "check"


def _cookie_status(_args, _cookie: str) -> None:
    output.print_info("\n".join(config.cookie_status_lines()))


def _cookie_set(args, _cookie: str) -> None:
    saved = config.save_cookie(_read_secret())
    output.print_info(f"已保存到 {config.CONFIG_PATH}")
    output.print_info("\n".join(config.cookie_status_lines()))
    if args.no_check:
        return
    try:
        label = client.check_login(saved)
    except client.AuthError as exc:
        raise client.AuthError(
            f"已保存到 {config.CONFIG_PATH}。服务器拒绝了这个 Cookie，"
            "请重新复制 LOFTER-PHONE-LOGIN-AUTH 或 LOFTER_SESS。"
        ) from exc
    except client.ApiError as exc:
        output.print_error(f"已保存。暂时无法确认登录：{exc}")
        return
    output.print_info(f"登录有效：{label}")


def _cookie_check(_args, cookie: str) -> None:
    output.print_info(f"登录有效：{client.check_login(cookie)}")


def _cookie_clear(_args, _cookie: str) -> None:
    if config.clear_cookie():
        output.print_info(f"已从 {config.CONFIG_PATH} 删除 Cookie。")
    else:
        output.print_info("没有已保存的 Cookie。")
    if os.environ.get(config.ENV_COOKIE, "").strip():
        output.print_info(f"环境变量 {config.ENV_COOKIE} 仍会生效。")


def _read_secret() -> str:
    if not _stdin_is_tty():
        return sys.stdin.read()
    # Windows 的隐藏输入不接收粘贴，还会把 Ctrl+V 写成控制字符。
    if os.name == "nt":
        print("粘贴 LOFTER-PHONE-LOGIN-AUTH 的值，或整段 Cookie，然后回车。", file=sys.stderr)
        print("这一步会显示在屏幕上，这样粘贴才不会损坏。", file=sys.stderr)
        try:
            return input("Cookie: ")
        except EOFError as exc:
            raise config.ConfigError("已取消。") from exc
    print("粘贴 LOFTER-PHONE-LOGIN-AUTH 的值，或整段 Cookie。输入不会显示。", file=sys.stderr)
    try:
        return getpass.getpass("Cookie: ")
    except EOFError as exc:
        raise config.ConfigError("已取消。") from exc


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def _collections(args, cookie: str) -> None:
    hint = "（没有更多了）" if args.offset > 0 else "（这里暂时没有内容）"
    if not args.collection_id:
        rows = client.get_collections(cookie, limit=args.limit, offset=args.offset)
        output.print_collection_list(rows, empty_message=hint)
        return
    posts = client.get_collection_posts(
        cookie,
        args.collection_id,
        limit=args.limit,
        offset=args.offset,
        order=args.order,
    )
    _show_posts(posts, empty_message=hint)


def _tag(args, cookie: str) -> None:
    posts = client.search_tag(cookie, args.tag, sort=args.sort, limit=args.limit, offset=args.offset)
    hint = "（没有更多了）" if args.offset > 0 else "（这里暂时没有内容）"
    _show_posts(posts, empty_message=hint)


def _recommend(args, cookie: str) -> None:
    _show_posts(client.recommend(cookie, args.count), empty_message="（暂时推荐不出来，稍后再试）")


def _random(args, cookie: str) -> None:
    _show_posts(
        client.sample_tag(cookie, args.tag, args.count, args.sort),
        empty_message="（这个标签暂时抽不到文字小说，换个标签或用 tag 看看）",
    )


def _post(args, cookie: str) -> None:
    post = client.get_post(cookie, args.post_id_or_url)
    convert = html_to_markdown if args.markdown else html_to_text
    post.text = convert(post.content_html)
    if not post.text:
        post.text = "（这篇没有可阅读的文字。图片和视频已略过。）"
    output.print_article(post)
    _remember(posts=[post])


def _show_posts(posts, empty_message: str = "（没有更多了）") -> None:
    output.print_post_list(posts, empty_message=empty_message)
    if posts:
        _remember(posts)


def _remember(posts) -> None:
    try:
        config.remember_posts(posts)
    except config.ConfigError as exc:
        output.print_error(str(exc))


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    sys.exit(main())
