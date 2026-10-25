"""LOFTER 网络层。地址、参数和请求头只改这个文件。"""

from __future__ import annotations

import html
import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config

PRODUCT = "lofter-android-7.6.12"
USER_AGENT = "LOFTER-Android 7.6.12 (V2272A; Android 13; null) WIFI"
URL_TAG_POSTS = "https://api.lofter.com/oldapi/tagPosts.api"
URL_POST_DETAIL = "https://api.lofter.com/oldapi/post/detail.api"
URL_COLLECTION = "https://api.lofter.com/v1.1/postCollection.api"
URL_SUBSCRIPTIONS = "https://api.lofter.com/newapi/subscribeCollection/list.json"
URL_HISTORY = "https://api.lofter.com/v2.0/history.api"
URL_ACCOUNT = "https://api.lofter.com/v1.1/usercounts.api"
URL_BLOG = "https://api.lofter.com/v2.0/blogHomePage.api"
URL_LIKES = "https://www.lofter.com/dwr/call/plaincall/MobilePostBean.getFavTrackItem.dwr"
URL_FOLLOWING = "https://www.lofter.com/dwr/call/plaincall/UserBean.getUserFollowingList.dwr"
URL_EXPLORE = "https://api.lofter.com/recommend/exploreRecom.json"
FALLBACK_HOT_TAGS = (
    "原创小说", "原创", "无CP", "短篇", "小说", "完结",
    "mujica", "初祥", "tksy", "海喵", "东方project", "八意永琳", "东方",
    "mygo", "海铃立希", "明日方舟", "少女前线", "原神", "永远的7日之都",
    "三角初华", "三角初音",
)
TAG_PAGE_SIZE = 12
MAX_PICKS = 5
RANDOM_PAGES = 3
NOVEL_TYPE = 1
SORT_TO_API = {"hot": "total", "new": "new", "month": "month"}
ORDER_TO_API = {"old": "1", "new": "0"}

TIMEOUT_SECONDS = 15.0
MIN_INTERVAL_SECONDS = 0.3
MAX_ATTEMPTS = 3
RETRY_HTTP = {429, 500, 502, 503, 504}
AUTH_MESSAGE = "Cookie 已失效，请重新从浏览器复制 LOFTER-PHONE-LOGIN-AUTH 或 LOFTER_SESS。"
_POST_URL = re.compile(
    r"(?:https?://)?(?:[\w.-]+\.)?lofter\.com/post/([0-9a-fA-F]+)_([0-9a-fA-F]+)",
    re.IGNORECASE,
)
_PERMALINK = re.compile(r"^([0-9a-fA-F]+)_([0-9a-fA-F]+)$")
_COLLECTION_URL = re.compile(r"[?&]collectionId=(\d+)", re.IGNORECASE)


class AuthError(RuntimeError):
    """登录态无效。"""


class ApiError(RuntimeError):
    """接口失败。"""


@dataclass
class Post:
    post_id: str
    title: str
    author: str
    hot: int
    blog_id: str = ""
    content_html: str = ""
    text: str = ""
    url: str = ""
    publish_time: int = 0
    post_type: int = 0


@dataclass
class Collection:
    collection_id: str
    name: str
    author: str
    post_count: int | None = None
    valid: bool = True


@dataclass
class Blog:
    blog_id: str
    name: str
    nickname: str
    url: str


class HTTPRequest:
    def __init__(self, method: str, url: str, params: dict | None = None, data=None, content: bytes | None = None, headers: dict | None = None) -> None:
        self.method = method
        self.url = _URLView(url, params or {})
        self.data = data
        self.content = content
        self.headers = headers or {}


class HTTPResponse:
    def __init__(self, status_code: int, content: bytes = b"", *, json=None, text: str | None = None) -> None:
        if json is not None:
            content = json_dumps(json)
        elif text is not None:
            content = text.encode("utf-8")
        self.status_code = status_code
        self.content = content

    def json(self):
        body = self.content.decode("utf-8") if self.content else "null"
        return json.loads(body)


class _URLView:
    def __init__(self, url: str, params: dict) -> None:
        self.params = {str(key): str(value) for key, value in params.items()}
        query = urllib.parse.urlencode(self.params)
        self.full = url + (("&" if "?" in url else "?") + query if query else "")


def json_dumps(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class LofterClient:
    def __init__(
        self,
        cookie: str,
        *,
        transport=None,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._cookie = cookie
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._last_request = 0.0
        self._headers = _headers(cookie)
        self._transport = transport

    def close(self) -> None:
        return None

    def __enter__(self) -> LofterClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_collections(self, limit: int = 50, offset: int = 0) -> list[Collection]:
        _check_window(limit, offset)

        def fetch(cursor: int, size: int):
            payload = self._request(
                "GET",
                URL_SUBSCRIPTIONS,
                params={"offset": cursor, "limit": size, "product": PRODUCT},
            )
            data = _dict(payload.get("data") or payload.get("response"))
            rows = data.get("collections") or []
            if not isinstance(rows, list):
                raise ApiError("订阅合集的返回结构和预期不一致，请只更新 client.py。")
            return rows, _as_int(data.get("offset")), _as_int(data.get("subscribeCollectionCount"))

        rows = _take_pages(fetch, limit, offset)
        return _map_rows(rows, _to_collection, "订阅合集的返回结构和预期不一致，请只更新 client.py。")

    def get_collection_posts(
        self,
        collection_id: str,
        limit: int = 20,
        offset: int = 0,
        order: str = "old",
    ) -> list[Post]:
        _check_window(limit, offset)
        collection_key = _collection_key(collection_id)
        api_order = ORDER_TO_API.get(order)
        if api_order is None:
            raise ApiError("排序只能是 old（从旧到新）或 new（从新到旧）。")

        def fetch(cursor: int, size: int):
            payload = self._request(
                "POST",
                URL_COLLECTION,
                params={"product": PRODUCT},
                data={
                    "method": "getCollectionDetail",
                    "offset": str(cursor),
                    "limit": str(size),
                    "collectionid": collection_key,
                    "order": api_order,
                },
            )
            data = _expect_response(payload)
            rows = data.get("items") or []
            if not isinstance(rows, list):
                raise ApiError("合集文章的返回结构和预期不一致，请只更新 client.py。")
            return rows, _as_int(data.get("offset")), _post_count(data.get("collection"))

        rows = _take_novels(fetch, limit, offset, page_cap=15)
        return _map_rows(
            rows,
            lambda row: _to_post(row, with_html=False),
            "合集文章的返回结构和预期不一致，请只更新 client.py。",
        )

    def search_tag(self, tag: str, sort: str = "hot", limit: int = 20, offset: int = 0, max_pages: int = 40) -> list[Post]:
        _check_window(limit, offset, ceiling=300)
        name = tag.strip()
        if not name:
            raise ApiError("标签不能为空。")
        api_sort = SORT_TO_API.get(sort)
        if api_sort is None:
            raise ApiError("排序只能是 hot、new 或 month。")

        need = offset + limit
        found: list[Post] = []
        seen: set[str] = set()
        cursor = 0
        before: int | None = None
        for _ in range(max_pages):
            if len(found) >= need:
                break
            payload = self._request(
                "POST",
                URL_TAG_POSTS,
                params={
                    "product": PRODUCT,
                    "method": "newTagSearch",
                    "offset": str(cursor),
                    "limit": str(TAG_PAGE_SIZE),
                    "firstpermalink": "null",
                    "tag": name,
                    "type": api_sort,
                },
                content=b"null",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = _expect_response(payload)
            rows = data.get("items") or []
            if not isinstance(rows, list) or not rows:
                break
            page_new = 0
            malformed = 0
            oldest: int | None = None
            for row in rows:
                try:
                    post = _to_post(row, with_html=False)
                except ApiError:
                    malformed += 1
                    continue
                if post.publish_time:
                    oldest = post.publish_time if oldest is None else min(oldest, post.publish_time)
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                page_new += 1
                if post.post_type != NOVEL_TYPE:
                    continue
                found.append(post)
            if page_new == 0:
                if malformed == len(rows) and not found:
                    raise ApiError("文章数据结构异常，请只更新 client.py。")
                break
            if oldest is not None and before is not None and oldest >= before and api_sort == "new":
                # 最新排序的时间不再变早，且游标不前进时停，避免空转。
                next_cursor = _as_int(data.get("offset"))
                if next_cursor is None or next_cursor <= cursor:
                    break
            if oldest is not None:
                before = oldest if before is None else min(before, oldest)
            next_cursor = _as_int(data.get("offset"))
            if next_cursor is None or next_cursor <= cursor:
                break
            cursor = next_cursor
        return found[offset : offset + limit]

    def get_post(self, post_id_or_url: str) -> Post:
        blog_id, post_id = _resolve_post(post_id_or_url)
        payload = self._request(
            "POST",
            URL_POST_DETAIL,
            params={"product": PRODUCT},
            data={
                "targetblogid": blog_id,
                "blogId": blog_id,
                "postid": post_id,
                "supportposttypes": "1,2,3,4,5,6",
                "offset": "0",
                "requestType": "1",
                "postdigestnew": "1",
                "checkpwd": "1",
                "needgetpoststat": "1",
            },
        )
        data = _expect_response(payload)
        rows = data.get("posts") or []
        if not isinstance(rows, list) or not rows:
            raise ApiError("没有拿到这篇文章。")
        return _to_post(rows[0])

    def get_history(self, limit: int = 20, offset: int = 0) -> list[Post]:
        _check_window(limit, offset)
        domain = self._blog_domain()

        def fetch(cursor: int, size: int):
            payload = self._request(
                "GET",
                URL_HISTORY,
                params={
                    "method": "getList",
                    "offset": cursor,
                    "limit": size,
                    "blogdomain": domain,
                    "product": PRODUCT,
                },
            )
            data = _expect_response(payload)
            rows = data.get("items") or []
            if not isinstance(rows, list):
                raise ApiError("浏览记录的返回结构和预期不一致，请只更新 client.py。")
            return rows, _as_int(data.get("offset")), _as_int(data.get("count"))

        posts = _map_rows(
            _take_novels(fetch, limit, offset),
            lambda row: _to_post(row, with_html=False),
            "浏览记录的返回结构和预期不一致，请只更新 client.py。",
        )
        return [post for post in posts if post.blog_id and post.blog_id != "0"]

    def get_likes(self, limit: int = 20, offset: int = 0) -> list[Post]:
        _check_window(limit, offset)

        def fetch(cursor: int, kept: int, target: int) -> list[Post]:
            size = min(20, max((target - kept) * 2, 5))
            text = self._dwr(
                URL_LIKES,
                "MobilePostBean",
                "getFavTrackItem",
                [f"number:{size}", f"number:{cursor}"],
                "https://www.lofter.com/like",
                "喜欢列表暂时不可用，请稍后再试。",
            )
            return _parse_fav_posts(text)

        return _collect(fetch, lambda post: post.post_id, lambda post: post.post_type == NOVEL_TYPE, limit, offset)

    def get_my_posts(self, limit: int = 20, offset: int = 0) -> list[Post]:
        return self.get_blog_posts(self._blog_domain(), limit, offset)

    def get_blog_posts(self, blog: str, limit: int = 20, offset: int = 0) -> list[Post]:
        _check_window(limit, offset)
        domain = _blog_domain_from_user(blog)

        def fetch(cursor: int, size: int):
            payload = self._request(
                "POST",
                URL_BLOG,
                params={"product": PRODUCT},
                data={
                    "method": "getPostLists",
                    "blogdomain": domain,
                    "offset": str(cursor),
                    "limit": str(size),
                    "supportposttypes": "1,2,3,4,5,6",
                    "postdigestnew": "1",
                    "returnData": "1",
                    "checkpwd": "1",
                    "needgetpoststat": "1",
                },
            )
            data = _expect_response(payload)
            rows = data.get("posts") or data.get("items") or []
            if not isinstance(rows, list):
                raise ApiError("博客文章的返回结构和预期不一致，请只更新 client.py。")
            return rows, _as_int(data.get("offset")), None

        return _map_rows(
            _take_novels(fetch, limit, offset),
            lambda row: _to_post(row, with_html=False),
            "博客文章的返回结构和预期不一致，请只更新 client.py。",
        )

    def get_following(self, limit: int = 20, offset: int = 0) -> list[Blog]:
        _check_window(limit, offset)

        def fetch(cursor: int, kept: int, target: int) -> list[Blog]:
            size = min(50, target - kept or 1)
            text = self._dwr(
                URL_FOLLOWING,
                "UserBean",
                "getUserFollowingList",
                [f"number:{size}", f"number:{cursor}"],
                "https://www.lofter.com/follow",
                "关注列表暂时不可用，请稍后再试。",
            )
            return _parse_following(text)

        return _collect(fetch, lambda blog: blog.blog_id, lambda _blog: True, limit, offset)

    def get_following_posts(self, limit: int = 20, offset: int = 0) -> list[Post]:
        _check_window(limit, offset)
        blogs = self.get_following(limit=min(100, max(limit + offset, 20)), offset=0)
        found: list[Post] = []
        seen: set[str] = set()
        for blog in blogs:
            try:
                batch = self.get_blog_posts(blog.name, limit=3, offset=0)
            except ApiError:
                continue
            for post in batch:
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                found.append(post)
            if len(found) >= offset + limit:
                break
        return found[offset : offset + limit]

    def sample_tag(self, tag: str, count: int = 1, sort: str = "hot", rng=None) -> list[Post]:
        _check_picks(count)
        return self._draw(tag, sort, count, rng or random)

    def recommend(self, count: int) -> list[Post]:
        _check_picks(count)
        liked = self.get_favorite_tags()
        hot = self.get_hot_tags()
        posts: list[Post] = []
        seen: set[str] = set()
        for tag in select_recommend_tags(count, liked, hot, random):
            if len(posts) >= count:
                break
            try:
                picked = self._draw(tag, "hot", 1, random)
            except ApiError:
                continue
            for post in picked:
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                posts.append(post)
        return posts

    def _draw(self, tag: str, sort: str, count: int, rng) -> list[Post]:
        """先抽一页，不够或没有小说时再看第 0 页。最多两次请求。"""
        api_sort = SORT_TO_API.get(sort)
        if api_sort is None:
            raise ApiError("排序只能是 hot、new 或 month。")
        name = tag.strip()
        if not name:
            raise ApiError("标签不能为空。")
        start = rng.randrange(RANDOM_PAGES)
        pages = [start] if start == 0 else [start, 0]
        pool: list[Post] = []
        seen: set[str] = set()
        for page in pages:
            for post in self._tag_page(name, api_sort, page * TAG_PAGE_SIZE):
                if post.post_type != NOVEL_TYPE or post.post_id in seen:
                    continue
                seen.add(post.post_id)
                pool.append(post)
            if len(pool) >= count:
                break
        if not pool:
            return []
        if len(pool) <= count:
            rng.shuffle(pool)
            return pool
        if count == 1:
            return [rng.choice(pool)]
        return rng.sample(pool, count)

    def _tag_page(self, tag: str, api_sort: str, offset: int) -> list[Post]:
        payload = self._request(
            "POST",
            URL_TAG_POSTS,
            params={
                "product": PRODUCT,
                "method": "newTagSearch",
                "offset": str(offset),
                "limit": str(TAG_PAGE_SIZE),
                "firstpermalink": "null",
                "tag": tag,
                "type": api_sort,
            },
            content=b"null",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        rows = _expect_response(payload).get("items") or []
        if not isinstance(rows, list):
            return []
        posts: list[Post] = []
        for row in rows:
            try:
                posts.append(_to_post(row, with_html=False))
            except ApiError:
                continue
        return posts

    def get_favorite_tags(self) -> list[str]:
        payload = self._request("GET", URL_ACCOUNT, params={"product": PRODUCT})
        return _tags_from_account(payload)

    def get_hot_tags(self) -> list[str]:
        try:
            payload = self._request("GET", URL_EXPLORE, params={"product": PRODUCT, "offset": 0})
        except ApiError:
            return list(FALLBACK_HOT_TAGS)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        found: list[str] = []
        for item in data.get("list") or []:
            if not isinstance(item, dict):
                continue
            post_data = item.get("postData") if isinstance(item.get("postData"), dict) else {}
            view = post_data.get("postView") if isinstance(post_data.get("postView"), dict) else {}
            tag_list = view.get("tagList") or []
            if isinstance(tag_list, str):
                tag_list = tag_list.split(",")
            if not isinstance(tag_list, list):
                continue
            found.extend(str(tag) for tag in tag_list)
        tags = _unique_tags(found)
        return tags or list(FALLBACK_HOT_TAGS)

    def _blog_domain(self) -> str:
        payload = self._request("GET", URL_ACCOUNT, params={"product": PRODUCT})
        data = _expect_response(payload)
        candidates: list[str] = []
        blogs = data.get("blogs") or []
        if isinstance(blogs, list):
            for blog in blogs:
                if not isinstance(blog, dict):
                    continue
                info = blog.get("blogInfo") if isinstance(blog.get("blogInfo"), dict) else blog
                candidates.append(str(info.get("homePageUrl") or ""))
                name = info.get("blogName")
                if name:
                    candidates.append(f"{name}.lofter.com")
        info = data.get("blogInfo") if isinstance(data.get("blogInfo"), dict) else {}
        candidates.append(str(info.get("homePageUrl") or ""))
        for raw in candidates:
            domain = _domain(raw)
            if domain:
                return domain
        raise ApiError("读不到当前账号的博客域名，浏览记录接口需要它。")

    def _request(self, method: str, url: str, **kwargs) -> dict:
        response = self._send(method, url, **kwargs)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ApiError("接口没有返回 JSON，可能已经失效。请只更新 client.py。") from exc
        decision = _decision(payload)
        if decision == "auth":
            raise AuthError(AUTH_MESSAGE)
        if decision == "bad":
            if _as_int((payload.get("meta") or {}).get("status") if isinstance(payload.get("meta"), dict) else None) == 4101:
                raise ApiError("喜欢列表暂时不可用。可以先用 history 查看浏览过的文章。")
            raise ApiError("接口拒绝了这次请求。可见范围有变，或 client.py 里的参数需要更新。")
        if not isinstance(payload, dict):
            raise ApiError("接口返回无法解析。")
        return payload

    def _request_text(self, method: str, url: str, **kwargs) -> str:
        response = self._send(method, url, **kwargs)
        text = response.content.decode("utf-8", "replace")
        if "login" in text.lower() and "blogName" not in text:
            raise AuthError(AUTH_MESSAGE)
        return text

    def _send(self, method: str, url: str, params=None, data=None, content=None, headers=None) -> HTTPResponse:
        last_error = "网络请求失败。"
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        for _attempt in range(self._max_attempts):
            self._pace()
            request = HTTPRequest(method, url, params or {}, data=data, content=content, headers=merged)
            try:
                response = self._transport(request) if self._transport else _urllib_send(request)
            except (TimeoutError, socket.timeout, urllib.error.URLError):
                last_error = "网络超时或连接失败，请稍后再试。"
                continue
            if response.status_code in (401, 403):
                raise AuthError(AUTH_MESSAGE)
            if response.status_code in RETRY_HTTP:
                last_error = f"服务暂时不可用（HTTP {response.status_code}）。"
                continue
            if response.status_code >= 400:
                raise ApiError(f"请求失败（HTTP {response.status_code}）。接口可能已变，请只更新 client.py。")
            if not (response.content or b"").strip():
                last_error = "服务返回了空内容，请稍后再试。"
                continue
            return response
        raise ApiError(last_error)

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request:
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self._last_request = now

    def _dwr(self, url: str, script: str, method: str, params: list[str], referer: str, failure: str) -> str:
        text = self._request_text(
            "POST",
            url,
            content=_dwr_body(script, method, params).encode("utf-8"),
            headers={"Content-Type": "text/plain", "Referer": referer},
        )
        if "Exception" in text or "allowScriptTagRemoting" in text:
            raise ApiError(failure)
        return text


def _run(cookie: str, method: str, /, *args, **kwargs):
    with LofterClient(cookie) as api:
        return getattr(api, method)(*args, **kwargs)


def get_collections(cookie: str, limit: int = 50, offset: int = 0) -> list[Collection]:
    return _run(cookie, "get_collections", limit, offset)


def get_collection_posts(
    cookie: str,
    collection_id: str,
    limit: int = 20,
    offset: int = 0,
    order: str = "old",
) -> list[Post]:
    return _run(cookie, "get_collection_posts", collection_id, limit, offset, order)


def search_tag(cookie: str, tag: str, sort: str = "hot", limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "search_tag", tag, sort, limit, offset)


def get_post(cookie: str, post_id_or_url: str) -> Post:
    return _run(cookie, "get_post", post_id_or_url)


def get_history(cookie: str, limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "get_history", limit, offset)


def get_likes(cookie: str, limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "get_likes", limit, offset)


def get_my_posts(cookie: str, limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "get_my_posts", limit, offset)


def get_following(cookie: str, limit: int = 20, offset: int = 0) -> list[Blog]:
    return _run(cookie, "get_following", limit, offset)


def get_following_posts(cookie: str, limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "get_following_posts", limit, offset)


def sample_tag(cookie: str, tag: str, count: int = 1, sort: str = "hot") -> list[Post]:
    return _run(cookie, "sample_tag", tag, count, sort)


def recommend(cookie: str, count: int) -> list[Post]:
    return _run(cookie, "recommend", count)


def select_recommend_tags(count: int, liked, hot, rng) -> list[str]:
    if count < 1:
        raise ApiError("篇数至少为 1。")
    liked_tags = _unique_tags(liked)
    liked_set = set(liked_tags)
    hot_tags = [tag for tag in _unique_tags(hot) if tag not in liked_set]
    chosen: list[str] = []
    if liked_tags:
        chosen.extend(rng.sample(liked_tags, min(count, len(liked_tags))))
    remain = count - len(chosen)
    if remain > 0 and hot_tags:
        chosen.extend(rng.sample(hot_tags, min(remain, len(hot_tags))))
    if not chosen:
        raise ApiError("没有可用的标签，暂时不能推荐。")
    return chosen


def check_login(cookie: str) -> str:
    with LofterClient(cookie) as client:
        payload = client._request("GET", URL_ACCOUNT, params={"product": PRODUCT})
    return _account_label(_expect_response(payload))


def _headers(cookie: str) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "lofproduct": PRODUCT,
        "Accept": "application/json",
    }
    raw = cookie.strip()
    try:
        raw.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ApiError("Cookie 里有无法放进请求头的字符。请重新运行 lftcli cookie set。") from exc
    phone_auth = ""
    if "=" in raw:
        cookie_header = raw
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip().upper() == "LOFTER-PHONE-LOGIN-AUTH":
                phone_auth = value.strip()
                break
    else:
        phone_auth = raw
        cookie_header = f"LOFTER-PHONE-LOGIN-AUTH={raw}"
    if not _header_has_controls(cookie_header):
        headers["Cookie"] = cookie_header
    if phone_auth and not _header_has_controls(phone_auth):
        headers["lofter-phone-login-auth"] = phone_auth
    return headers


def _header_has_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _urllib_send(request: HTTPRequest) -> HTTPResponse:
    headers = dict(request.headers)
    payload = request.content
    if request.data is not None and payload is None:
        payload = urllib.parse.urlencode(request.data).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    outgoing = urllib.request.Request(request.url.full, data=payload, headers=headers, method=request.method)
    try:
        with urllib.request.urlopen(outgoing, timeout=TIMEOUT_SECONDS) as response:
            return HTTPResponse(response.status, response.read())
    except urllib.error.HTTPError as exc:
        return HTTPResponse(exc.code, exc.read())


def _collect(fetch, key, keep, limit: int, offset: int):
    kept = []
    seen: set[str] = set()
    cursor = 0
    target = offset + limit
    for _ in range(40):
        if len(kept) >= target:
            break
        batch = fetch(cursor, len(kept), target)
        if not batch:
            break
        fresh = 0
        for item in batch:
            ident = key(item)
            if ident in seen:
                continue
            seen.add(ident)
            fresh += 1
            if keep(item):
                kept.append(item)
        if fresh == 0:
            break
        cursor += len(batch)
    return kept[offset : offset + limit]


def _dwr_body(script: str, method: str, params: list[str]) -> str:
    lines = [
        "callCount=1",
        "scriptSessionId=${scriptSessionId}187",
        "httpSessionId=",
        f"c0-scriptName={script}",
        f"c0-methodName={method}",
        "c0-id=0",
    ]
    lines.extend(f"c0-param{index}={value}" for index, value in enumerate(params))
    lines.append("batchId=1")
    return "\n".join(lines)


def _parse_fav_posts(text: str) -> list[Post]:
    objects = _dwr_objects(text)
    match = re.search(r"_remoteHandleCallback\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*\[([^\]]*)\]\s*\)", text)
    if not match:
        return []
    posts: list[Post] = []
    for ref in re.findall(r"s(\d+)", match.group(1)):
        source = objects.get(ref) or {}
        post_ref = source.get("post")
        if isinstance(post_ref, str) and post_ref in objects:
            source = objects[post_ref]
        post_id = source.get("id")
        blog_id = source.get("blogId")
        post_type = _as_int(source.get("type"))
        if post_id in (None, "") or post_type is None:
            continue
        info_ref = source.get("blogInfo")
        info = objects.get(info_ref) if isinstance(info_ref, str) else {}
        if not isinstance(info, dict):
            info = {}
        hot = _as_int(source.get("hot"))
        if hot is None:
            hot = _as_int(source.get("postHot")) or 0
        posts.append(
            _to_post(
                {
                    "id": post_id,
                    "blogId": blog_id or "",
                    "type": post_type,
                    "title": source.get("title") or "",
                    "noticeLinkTitle": source.get("noticeLinkTitle") or "",
                    "digest": source.get("digest") or "",
                    "hot": hot,
                    "blogPageUrl": source.get("blogPageUrl") or "",
                    "permalink": source.get("permalink") or "",
                    "publishTime": source.get("publishTime") or 0,
                    "blogInfo": {"blogNickName": info.get("blogNickName") or info.get("blogName") or ""},
                },
                with_html=False,
            )
        )
    return posts


def _dwr_objects(text: str) -> dict[str, dict]:
    objects: dict[str, dict] = {}
    for match in re.finditer(r"(?<![\w])s(\d+)\.([A-Za-z_][A-Za-z0-9_]*)=s(\d+);", text):
        objects.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
    for match in re.finditer(r"(?<![\w])s(\d+)\.([A-Za-z_][A-Za-z0-9_]*)=(-?\d+);", text):
        objects.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
    for match in re.finditer(r'(?<![\w])s(\d+)\.([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"\\])*)";', text):
        if match.group(2) in {"content", "digest", "embed", "photoLinks"}:
            continue
        objects.setdefault(match.group(1), {})[match.group(2)] = _dwr_unescape(match.group(3))
    return objects


def _parse_following(text: str) -> list[Blog]:
    blogs: dict[str, dict[str, str]] = {}
    patterns = {
        "name": re.compile(r's(\d+)\.blogName="((?:\\.|[^"\\])*)"'),
        "nickname": re.compile(r's(\d+)\.blogNickName="((?:\\.|[^"\\])*)"'),
        "blog_id": re.compile(r"s(\d+)\.blogId=(\d+);"),
        "url": re.compile(r's(\d+)\.homePageUrl="((?:\\.|[^"\\])*)"'),
    }
    for field, pattern in patterns.items():
        for match in pattern.finditer(text):
            blogs.setdefault(match.group(1), {})[field] = _dwr_unescape(match.group(2))
    found: list[Blog] = []
    for item in blogs.values():
        blog_id = item.get("blog_id") or ""
        name = item.get("name") or ""
        if not blog_id or not name:
            continue
        found.append(Blog(blog_id=blog_id, name=name, nickname=item.get("nickname") or name, url=item.get("url") or f"https://{name}.lofter.com"))
    return found


def _dwr_unescape(value: str) -> str:
    return _decode_escapes(value)


def _decode_escapes(value: str) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(0)
        if token.startswith("\\u"):
            return chr(int(token[2:], 16))
        return {"\\n": "\n", "\\r": "\r", "\\t": "\t", '\\"': '"', "\\/": "/", "\\\\": "\\"}.get(token, token[1:])

    return re.sub(r"\\u[0-9a-fA-F]{4}|\\.", replace, value)


def _article_html(raw: str) -> str:
    text = _decode_escapes(raw)
    if "&lt;" in text:
        text = html.unescape(text)
    return text


def _unique_tags(values) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for value in values or []:
        name = _clean_tag(value)
        if not name or name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def _clean_tag(value) -> str:
    if isinstance(value, dict):
        for key in ("tagName", "name", "tag", "keyName"):
            if value.get(key):
                value = value[key]
                break
        else:
            return ""
    text = " ".join(str(value or "").split())
    if not text or len(text) > 30:
        return ""
    return text.lstrip("#")


def _tags_from_account(payload: dict) -> list[str]:
    data = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    found: list[str] = []
    found.extend(_collect_tag_values(data.get("manageTags")))
    for blog in data.get("blogs") or []:
        if not isinstance(blog, dict):
            continue
        info = blog.get("blogInfo") if isinstance(blog.get("blogInfo"), dict) else {}
        found.append(info.get("keyTag") or "")
    return _unique_tags(found)


def _collect_tag_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [_clean_tag(value)]
    if isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(_collect_tag_values(item))
        return found
    return []


def _blog_domain_from_user(raw: str) -> str:
    text = raw.strip().strip("/")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split("/", 1)[0]
    if text.endswith(".lofter.com"):
        name = text[: -len(".lofter.com")]
    else:
        name = text
    if not re.fullmatch(r"[\w-]+", name):
        raise ApiError("博客名只能包含字母、数字、下划线和连字符。")
    return f"{name}.lofter.com"


def _decision(payload) -> str:
    if not isinstance(payload, dict):
        return "bad"
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    status = _as_int(meta.get("status"))
    code = _as_int(payload.get("code"))
    if status in (401, 403) or code in (401, 403):
        return "auth"
    if status in RETRY_HTTP or code in RETRY_HTTP:
        return "retry"
    if status == 4202:
        raise ApiError("这篇不存在或已删除。")
    if status not in (None, 200):
        return "bad"
    if code not in (None, 0, 200):
        return "bad"
    return "ok"


def _take_pages(fetch, limit: int, offset: int, page_cap: int = 50) -> list:
    rows: list = []
    seen: set[str] = set()
    cursor = offset
    while len(rows) < limit:
        size = min(page_cap, limit - len(rows))
        batch, nxt, total = fetch(cursor, size)
        if not batch:
            break
        fresh: list = []
        for row in batch:
            key = _row_key(row)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            fresh.append(row)
        if not fresh:
            break
        rows.extend(fresh)
        if isinstance(nxt, int) and nxt > cursor:
            cursor = nxt
        else:
            cursor += len(batch)
        if isinstance(total, int) and cursor >= total:
            break
        if len(batch) < size:
            break
    return rows[:limit]


def _take_novels(fetch, limit: int, skip: int, page_cap: int = 50) -> list:
    kept: list = []
    seen: set[str] = set()
    cursor = 0
    target = skip + limit
    for _ in range(40):
        if len(kept) >= target:
            break
        batch, nxt, total = fetch(cursor, page_cap)
        if not batch:
            break
        fresh = 0
        for row in batch:
            key = _row_key(row)
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            fresh += 1
            if not _is_novel_row(row):
                continue
            kept.append(row)
        if fresh == 0:
            break
        if isinstance(nxt, int) and nxt > cursor:
            cursor = nxt
        else:
            cursor += len(batch)
        if isinstance(total, int) and cursor >= total:
            break
        if len(batch) < page_cap:
            break
    return kept[skip : skip + limit]


def _is_novel_row(row) -> bool:
    if not isinstance(row, dict):
        return False
    source = row.get("post") if isinstance(row.get("post"), dict) else row
    return _as_int(source.get("type")) == NOVEL_TYPE


def _map_rows(rows: list, convert, message: str) -> list:
    mapped = []
    for row in rows:
        try:
            mapped.append(convert(row))
        except ApiError:
            continue
    if rows and not mapped:
        raise ApiError(message)
    return mapped


def _row_key(row) -> str | None:
    if not isinstance(row, dict):
        return None
    sources = [row]
    for name in ("post", "collection"):
        nested = row.get(name)
        if isinstance(nested, dict):
            sources.insert(0, nested)
    for source in sources:
        for key in ("id", "postId", "collectionId"):
            value = source.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
    return None


def _to_post(row: dict, *, with_html: bool = True) -> Post:
    if not isinstance(row, dict):
        raise ApiError("文章数据结构异常，请只更新 client.py。")
    source = row.get("post") if isinstance(row.get("post"), dict) else row
    post_id = source.get("id")
    if post_id is None:
        post_id = source.get("postId")
    if post_id is None or post_id == "":
        raise ApiError("文章缺少 ID，请只更新 client.py。")
    blog_id = source.get("blogId")
    if blog_id is None:
        blog_id = source.get("blogid") or ""
    if blog_id in ("", "0", 0):
        blog_id = _blog_id_from_permalink(str(source.get("permalink") or source.get("blogPageUrl") or ""))
    counts = source.get("postCount") if isinstance(source.get("postCount"), dict) else {}
    hot = _as_int(source.get("hot"))
    if hot is None:
        hot = _as_int(counts.get("postHot"))
    if hot is None or hot < 0:
        hot = 0
    content = ""
    if with_html:
        raw = source.get("content") or source.get("caption") or ""
        content = _article_html(raw if isinstance(raw, str) else "")
    return Post(
        post_id=str(post_id),
        blog_id=str(blog_id),
        title=_title(source),
        author=_author(source),
        hot=hot,
        content_html=content,
        url=source.get("blogPageUrl") or "",
        publish_time=_as_int(source.get("publishTime")) or 0,
        post_type=_as_int(source.get("type")) or 0,
    )


def _to_collection(row: dict) -> Collection:
    if not isinstance(row, dict):
        raise ApiError("合集数据结构异常，请只更新 client.py。")
    nested = row.get("collection") if isinstance(row.get("collection"), dict) else row
    collection_id = _present(nested, "collectionId", "id")
    if collection_id is None:
        collection_id = _present(row, "collectionId", "id")
    if collection_id is None:
        raise ApiError("合集缺少 ID，请只更新 client.py。")
    info = row.get("blogInfo") if isinstance(row.get("blogInfo"), dict) else {}
    author = (
        row.get("blogNickName")
        or nested.get("blogNickName")
        or info.get("blogNickName")
        or "未知"
    )
    count = _as_int(nested.get("postCount") if nested.get("postCount") is not None else row.get("postCount"))
    valid = row.get("valid")
    if valid is None:
        valid = nested.get("valid", True)
    return Collection(
        collection_id=str(collection_id),
        name=_one_line(nested.get("name") or row.get("name") or "未命名合集"),
        author=_one_line(str(author)),
        post_count=count,
        valid=bool(valid),
    )


def _title(source: dict) -> str:
    title = _one_line(source.get("title") or source.get("noticeLinkTitle") or "")
    if title:
        return title
    digest = _one_line(source.get("digest") or "")
    if digest:
        return digest[:24] + ("…" if len(digest) > 24 else "")
    return "无标题"


def _author(source: dict) -> str:
    info = source.get("blogInfo") if isinstance(source.get("blogInfo"), dict) else {}
    return _one_line(info.get("blogNickName") or info.get("blogName") or "未知")


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _present(source: dict, *keys: str):
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if value is None or value == "":
            continue
        return value
    return None


def _resolve_post(raw: str) -> tuple[str, str]:
    text = raw.strip()
    match = _POST_URL.search(text) or _PERMALINK.fullmatch(text)
    if match:
        return str(int(match.group(1), 16)), str(int(match.group(2), 16))
    if text.isdigit():
        found = config.lookup_post(text)
        if not found:
            raise ApiError(
                "这个数字 ID 还不在本地目录里。先用 tag、collections、likes、following 或 history 列出它，"
                "或者直接传入文章链接（…/post/博客ID_文章ID）。"
            )
        return str(found["blog_id"]), text
    raise ApiError("无法识别的文章。请传文章链接，或列表里出现过的数字 ID。")


def _blog_id_from_permalink(raw: str) -> str:
    match = _POST_URL.search(raw) or _PERMALINK.fullmatch(raw.strip())
    if not match:
        return ""
    return str(int(match.group(1), 16))


def _collection_key(raw: str) -> str:
    text = raw.strip()
    if text.isdigit():
        return text
    match = _COLLECTION_URL.search(text)
    if match:
        return match.group(1)
    raise ApiError("无法识别的合集。请传数字 ID，或带 collectionId= 的链接。")


def _account_label(data: dict) -> str:
    nick = ""
    domain = ""
    blogs = data.get("blogs") if isinstance(data.get("blogs"), list) else []
    for blog in blogs:
        if not isinstance(blog, dict):
            continue
        info = blog.get("blogInfo") if isinstance(blog.get("blogInfo"), dict) else blog
        nick = nick or _one_line(info.get("blogNickName") or "")
        domain = domain or _domain(str(info.get("homePageUrl") or ""))
        if not domain and info.get("blogName"):
            domain = _domain(f"{info['blogName']}.lofter.com")
        if nick and domain:
            break
    if nick and domain:
        return f"{nick}（{domain}）"
    if nick or domain:
        return nick or domain
    raise ApiError("登录请求成功，但没有读到账号信息。")


def _expect_response(payload: dict) -> dict:
    data = payload.get("response")
    if isinstance(data, dict):
        return data
    raise ApiError("接口没有 response 字段，请只更新 client.py。")


def _post_count(collection) -> int | None:
    if isinstance(collection, dict):
        return _as_int(collection.get("postCount"))
    return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _domain(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlparse(text).netloc
    text = text.strip("/")
    if text.endswith(".lofter.com"):
        return text
    return ""


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check_picks(count: int) -> None:
    if count < 1 or count > MAX_PICKS:
        raise ApiError(f"篇数需要在 1 到 {MAX_PICKS} 之间。")


def _check_window(limit: int, offset: int, ceiling: int = 100) -> None:
    if limit < 1 or limit > ceiling:
        raise ApiError(f"条数需要在 1 到 {ceiling} 之间。")
    if offset < 0:
        raise ApiError("偏移不能是负数。")
    if offset + limit > 300:
        raise ApiError("一次最多向后看 300 条，请减小偏移或条数。")
