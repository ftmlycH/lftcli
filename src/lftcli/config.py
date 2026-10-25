"""本机 Cookie，以及数字文章 ID 对应的博客 ID。"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".lofter-cli"
CONFIG_PATH = CONFIG_DIR / "config.json"
INDEX_PATH = CONFIG_DIR / "posts.json"
ENV_COOKIE = "LOFTER_COOKIE"
_INDEX_LIMIT = 2000
_COOKIE_MAX = 16384


class ConfigError(RuntimeError):
    """配置缺失或无法读取。"""


def load_cookie() -> str | None:
    env = os.environ.get(ENV_COOKIE, "").strip()
    if env:
        return env
    return load_saved_cookie()


def load_saved_cookie() -> str | None:
    data = _read_config()
    cookie = data.get("cookie") if isinstance(data, dict) else None
    if isinstance(cookie, str) and cookie.strip():
        return cookie.strip()
    return None


def require_cookie() -> str:
    cookie = load_cookie()
    if cookie:
        return cookie
    raise ConfigError(
        "未配置 Cookie，已停止，不会发起网络请求。\n"
        "运行 lftcli cookie set ，按提示粘贴 LOFTER-PHONE-LOGIN-AUTH 或 LOFTER_SESS。\n"
        f"也可以设置环境变量 {ENV_COOKIE}。本程序不会自动登录，也不处理验证码。"
    )


def save_cookie(cookie: str) -> str:
    value = normalize_cookie(cookie)
    _reject_header_unsafe(value)
    data = _read_config()
    data["cookie"] = value
    data.pop("blog_domain", None)
    data.pop("favorite_tags", None)
    data.pop("tag_depths", None)
    data.pop("following_blogs", None)
    data.pop("following_posts", None)
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if os.name != "nt":
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    return value


def clear_cookie() -> bool:
    data = _read_config()
    changed = False
    for key in ("cookie", "blog_domain", "favorite_tags", "tag_depths", "following_blogs", "following_posts"):
        if key in data:
            del data[key]
            changed = True
    if not changed:
        return False
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def load_blog_domain() -> str | None:
    data = _read_config()
    value = data.get("blog_domain") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def save_blog_domain(domain: str) -> None:
    text = str(domain or "").strip()
    if not text:
        return
    data = _read_config()
    if data.get("blog_domain") == text:
        return
    data["blog_domain"] = text
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_favorite_tags() -> list[str]:
    data = _read_config()
    value = data.get("favorite_tags") if isinstance(data, dict) else None
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def save_favorite_tags(tags: list[str]) -> None:
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    data = _read_config()
    if data.get("favorite_tags") == cleaned:
        return
    data["favorite_tags"] = cleaned
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_tag_depth(tag: str, sort: str) -> int | None:
    data = _read_config()
    depths = data.get("tag_depths") if isinstance(data, dict) else None
    if not isinstance(depths, dict):
        return None
    value = depths.get(f"{tag}|{sort}")
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return None
    return depth if depth > 0 else None


def save_tag_depth(tag: str, sort: str, depth: int) -> None:
    try:
        value = int(depth)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    data = _read_config()
    depths = data.get("tag_depths")
    if not isinstance(depths, dict):
        depths = {}
    key = f"{tag}|{sort}"
    if depths.get(key) == value:
        return
    depths[key] = value
    data["tag_depths"] = depths
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_following_blogs() -> list[dict]:
    data = _read_config()
    rows = data.get("following_blogs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        blog_id = str(row.get("blog_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not blog_id or not name:
            continue
        found.append(
            {
                "blog_id": blog_id,
                "name": name,
                "nickname": str(row.get("nickname") or name).strip() or name,
                "url": str(row.get("url") or "").strip(),
            }
        )
    return found


def save_following_blogs(blogs) -> None:
    rows: list[dict] = []
    for blog in blogs:
        blog_id = str(getattr(blog, "blog_id", "") or "").strip()
        name = str(getattr(blog, "name", "") or "").strip()
        if not blog_id or not name:
            continue
        rows.append(
            {
                "blog_id": blog_id,
                "name": name,
                "nickname": str(getattr(blog, "nickname", "") or name).strip() or name,
                "url": str(getattr(blog, "url", "") or "").strip(),
            }
        )
        if len(rows) >= 80:
            break
    data = _read_config()
    if data.get("following_blogs") == rows:
        return
    data["following_blogs"] = rows
    _atomic_write(CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def cookie_status_lines() -> list[str]:
    lines = [f"配置文件：{CONFIG_PATH}"]
    try:
        saved = load_saved_cookie()
    except ConfigError as exc:
        lines.append(f"文件：读不了（{exc}）")
        saved = None
    else:
        lines.append("文件：未保存" if not saved else f"文件：已保存（{_cookie_label(saved)}）")
    env = os.environ.get(ENV_COOKIE, "").strip()
    if env:
        lines.append(f"环境变量 {ENV_COOKIE}：已设置（{_cookie_label(env)}），会盖过配置文件")
        lines.append("当前生效：环境变量")
    elif saved:
        lines.append(f"环境变量 {ENV_COOKIE}：未设置")
        lines.append("当前生效：配置文件")
    else:
        lines.append(f"环境变量 {ENV_COOKIE}：未设置")
        lines.append("当前生效：无")
    return lines


def normalize_cookie(cookie: str) -> str:
    text = _strip_quotes(str(cookie or ""))
    if text.lower().startswith("cookie:"):
        text = _strip_quotes(text.split(":", 1)[1])
    lines = [line.strip().strip(";") for line in text.splitlines() if line.strip()]
    text = lines[0] if len(lines) == 1 else "; ".join(lines)
    text = text.strip()
    if not text:
        raise ConfigError("Cookie 是空的。")
    if len(text) < 8:
        raise ConfigError("这段内容太短，不像 Cookie。")
    if len(text) > _COOKIE_MAX:
        raise ConfigError("这段内容太长，不像 Cookie。")
    if text.lower().startswith(("http://", "https://")):
        raise ConfigError("这是链接，不是 Cookie。请粘贴 LOFTER-PHONE-LOGIN-AUTH 或 LOFTER_SESS 的值。")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ConfigError(
            "这次粘贴损坏了，里面有控制字符。隐藏输入吃掉了 Ctrl+V。"
            "请重新运行 lftcli cookie set，在会显示内容的提示里再粘贴一次。"
        )
    return text


def _strip_quotes(text: str) -> str:
    text = text.strip()
    changed = True
    while changed and text:
        changed = False
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
            changed = True
            continue
        if text[0] in {'"', "'"} or text[-1] in {'"', "'"}:
            text = text.strip("\"'").strip()
            changed = True
    return text


def _cookie_label(cookie: str) -> str:
    upper = cookie.upper()
    if "LOFTER-PHONE-LOGIN-AUTH=" in upper or "=" not in cookie:
        kind = "LOFTER-PHONE-LOGIN-AUTH"
    elif "LOFTER_SESS=" in upper:
        kind = "LOFTER_SESS"
    else:
        kind = "浏览器 Cookie"
    return f"{kind}，长度 {len(cookie)}"


def _reject_header_unsafe(cookie: str) -> None:
    try:
        cookie.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ConfigError("Cookie 里有无法放进请求头的字符。请只粘贴 Cookie 值，不要带中文说明。") from exc


def _read_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"无法读取 {CONFIG_PATH}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"无法解析 {CONFIG_PATH}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} 的内容不是对象。")
    return data


def remember_posts(posts) -> None:
    index = _load_index()
    changed = False
    for post in posts:
        post_id = str(getattr(post, "post_id", "") or "")
        blog_id = str(getattr(post, "blog_id", "") or "")
        if not post_id or not blog_id or blog_id == "0":
            continue
        entry = {"blog_id": blog_id, "url": getattr(post, "url", "") or ""}
        if index.get(post_id) != entry:
            index[post_id] = entry
            changed = True
    if not changed:
        return
    if len(index) > _INDEX_LIMIT:
        overflow = len(index) - _INDEX_LIMIT
        for key in list(index)[:overflow]:
            del index[key]
    try:
        _atomic_write(INDEX_PATH, json.dumps(index, ensure_ascii=False))
    except OSError as exc:
        raise ConfigError(f"无法写入阅读目录 {INDEX_PATH}：{exc}") from exc


def lookup_post(post_id: str) -> dict | None:
    found = _load_index().get(str(post_id))
    if isinstance(found, dict) and found.get("blog_id"):
        return found
    return None


def _load_index() -> dict:
    if not INDEX_PATH.is_file():
        return {}
    try:
        raw = INDEX_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        backup = INDEX_PATH.with_name(INDEX_PATH.name + ".bak")
        try:
            INDEX_PATH.replace(backup)
        except OSError:
            return {}
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        # 清理孤立代理项，避免 Windows 下 utf-8 编码失败。
        safe = text.encode("utf-8", "replace").decode("utf-8")
        tmp.write_text(safe, encoding="utf-8", newline="\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
