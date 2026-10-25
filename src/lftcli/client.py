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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "原创小说", "原创", "无CP", "短篇", "小说", "完结", "竹林组",
    "mujica", "初祥", "tksy", "海喵", "东方project", "八意永琳", "东方",
    "mygo", "海铃立希", "明日方舟", "少女前线", "原神", "永远的7日之都",
    "三角初华", "三角初音", "鸣潮", "崩坏3", "第五人格", "王者荣耀", "赛马娘", "lovelive", "少女与战车",
)
TAG_PAGE_SIZE = 20
# 标签接口实际常见步长（limit 再大也常返回约 12 条）。
TAG_CURSOR_STEP = 12
MAX_PICKS = 5
SAMPLE_POOL = 12
# 随机抽样固定游标上界（约页数 * 12），不落盘、不自适应。
TAG_DEPTH_DEFAULT = 600
NOVEL_TYPE = 1
# 标签流里筛小说，避免热门页被图片占满后空翻十几页。
TAG_POST_TYPES = str(NOVEL_TYPE)
SORT_TO_API = {"hot": "total", "new": "new", "month": "month"}
ORDER_TO_API = {"old": "1", "new": "0"}

TIMEOUT_SECONDS = 8.0
# 单条 CLI 指令总预算：列表/随机/推荐可到约 15 秒，不再依赖磁盘缓存。
COMMAND_BUDGET_SECONDS = 15.0
MIN_INTERVAL_SECONDS = 0.03
MAX_ATTEMPTS = 3
PAGE_CAP = 40
RETRY_HTTP = {429, 500, 502, 503, 504}
_SYSTEM_RNG = random.SystemRandom()
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
        budget: float | None = COMMAND_BUDGET_SECONDS,
    ) -> None:
        self._cookie = cookie
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._last_request = 0.0
        self._headers = _headers(cookie)
        self._transport = transport
        self._budget = budget
        self._started = time.monotonic()
        self._domain: str | None = None
        self._favorite_tags: list[str] | None = None

    def close(self) -> None:
        return None

    def __enter__(self) -> LofterClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _remaining(self) -> float:
        if self._budget is None:
            return TIMEOUT_SECONDS
        return self._budget - (time.monotonic() - self._started)

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

        rows = _take_novels(fetch, limit, offset, page_cap=PAGE_CAP)
        return _map_rows(
            rows,
            lambda row: _to_post(row, with_html=False),
            "合集文章的返回结构和预期不一致，请只更新 client.py。",
        )

    def search_tag(
        self,
        tag: str,
        sort: str = "hot",
        limit: int = 20,
        offset: int = 0,
        max_pages: int | None = None,
        start_cursor: int = 0,
    ) -> list[Post]:
        _check_window(limit, offset, ceiling=300)
        name = tag.strip()
        if not name:
            raise ApiError("标签不能为空。")
        api_sort = SORT_TO_API.get(sort)
        if api_sort is None:
            raise ApiError("排序只能是 hot、new 或 month。")
        if start_cursor < 0:
            raise ApiError("起始游标不能是负数。")

        # postTypes=1 后 API offset 约等于小说偏移；从目标 offset 起跳，只取 limit 篇。
        origin = start_cursor if start_cursor else offset
        pages = max_pages if max_pages is not None else min(
            50, max(1, (limit + TAG_CURSOR_STEP - 1) // TAG_CURSOR_STEP)
        )
        if pages > 1 and self._transport is None:
            starts = [origin + index * TAG_CURSOR_STEP for index in range(pages)]
            batches = self._fetch_tag_windows_parallel(name, sort, starts)
            found: list[Post] = []
            seen: set[str] = set()
            for batch in batches:
                for post in batch:
                    if post.post_id in seen:
                        continue
                    seen.add(post.post_id)
                    found.append(post)
                    if len(found) >= limit:
                        break
                if len(found) >= limit:
                    break
            return found[:limit]
        return self._search_tag_sequential(name, sort, api_sort, origin, limit, pages)

    def _search_tag_sequential(
        self,
        name: str,
        sort: str,
        api_sort: str,
        origin: int,
        limit: int,
        pages: int,
    ) -> list[Post]:
        found: list[Post] = []
        seen: set[str] = set()
        cursor = origin
        before: int | None = None
        for _ in range(pages):
            if len(found) >= limit:
                break
            if self._remaining() < 0.25:
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
                    "postTypes": TAG_POST_TYPES,
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
            next_cursor = _as_int(data.get("offset"))
            if oldest is not None and before is not None and oldest >= before and api_sort == "new":
                if next_cursor is None or next_cursor <= cursor:
                    break
            if oldest is not None:
                before = oldest if before is None else min(before, oldest)
            if next_cursor is None or next_cursor <= cursor:
                break
            cursor = next_cursor
        return found[:limit]

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

        # 从 offset 起最多翻 1 页，避免历史+域名把总时长拖过预算。
        posts = _map_rows(
            _take_novels(
                fetch,
                limit,
                0,
                page_cap=max(limit, TAG_PAGE_SIZE),
                max_pages=1,
                start_cursor=offset,
            ),
            lambda row: _to_post(row, with_html=False),
            "浏览记录的返回结构和预期不一致，请只更新 client.py。",
        )
        # 没有 blog_id 的仍保留，否则翻页时容易被滤成「没有结果」。
        return [post for post in posts if post.post_id]

    def get_likes(self, limit: int = 20, offset: int = 0) -> list[Post]:
        _check_window(limit, offset)
        # 一次 DWR：从 offset 起多取一点再滤小说，避免连环翻页。
        size = min(40, max((offset + limit) * 2, 10))
        text = self._dwr(
            URL_LIKES,
            "MobilePostBean",
            "getFavTrackItem",
            [f"number:{size}", f"number:{offset}"],
            "https://www.lofter.com/like",
            "喜欢列表暂时不可用，请稍后再试。",
        )
        posts = [post for post in _parse_fav_posts(text) if post.post_type == NOVEL_TYPE]
        seen: set[str] = set()
        kept: list[Post] = []
        for post in posts:
            if post.post_id in seen:
                continue
            seen.add(post.post_id)
            kept.append(post)
            if len(kept) >= limit:
                break
        return kept

    def get_my_posts(self, limit: int = 20, offset: int = 0) -> list[Post]:
        return self.get_blog_posts(self._blog_domain(), limit, offset)

    def get_blog_posts(self, blog: str, limit: int = 20, offset: int = 0, max_pages: int | None = None) -> list[Post]:
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
            _take_novels(
                fetch,
                limit,
                offset,
                page_cap=max(limit + offset, 10),
                max_pages=max_pages,
                max_empty_novel_pages=2,
            ),
            lambda row: _to_post(row, with_html=False),
            "博客文章的返回结构和预期不一致，请只更新 client.py。",
        )

    def get_following(self, limit: int = 20, offset: int = 0) -> list[Blog]:
        _check_window(limit, offset)

        def fetch(cursor: int, kept: int, target: int) -> list[Blog]:
            size = min(50, max(target - kept, 1))
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

    def get_following_blog_posts(self, blog_key: str, limit: int = 20, offset: int = 0) -> list[Post]:
        """按关注博主 ID / blogName 列出其文字小说。"""
        name = self._resolve_following_blog(blog_key)
        need = offset + limit
        pages = min(8, max(1, (need + 19) // 20))
        return self.get_blog_posts(name, limit=limit, offset=offset, max_pages=pages)

    def _resolve_following_blog(self, blog_key: str) -> str:
        key = str(blog_key or "").strip()
        if not key:
            raise ApiError("请提供关注博主的 ID 或 blogName。")
        if not key.isdigit():
            return key
        # 现场翻关注列表解析 ID → blogName，不落盘。
        cursor = 0
        page = 50
        while self._remaining() >= 0.8:
            blogs = self.get_following(limit=page, offset=cursor)
            if not blogs:
                break
            for blog in blogs:
                if blog.blog_id == key:
                    return blog.name
            if len(blogs) < page:
                break
            cursor += page
            if cursor >= 300:
                break
        raise ApiError("关注列表里找不到这个 ID。先运行 following 查看名单。")

    def sample_tag(self, tag: str, count: int = 1, sort: str = "hot", rng=None) -> list[Post]:
        """在固定深度窗口内系统熵选游标，真随机抽取；空窗则回落首页。"""
        _check_picks(count)
        rng = rng or _SYSTEM_RNG
        posts = self._sample_from_tag(tag, count, sort, rng)
        if posts or count != 1 or self._remaining() < 0.5:
            return posts
        pool = self._fetch_tag_window(tag.strip(), sort, 0)
        return _pick(pool, 1, rng)

    def recommend(self, count: int, rng=None) -> list[Post]:
        """现场拉喜欢标签，随机选 count 个，各在小说流里真随机抽 1 篇。"""
        _check_picks(count)
        rng = rng or _SYSTEM_RNG
        liked = self.get_favorite_tags() or list(FALLBACK_HOT_TAGS)
        if not liked:
            return []
        wave = min(len(liked), count + (1 if count < MAX_PICKS else 0))
        tags = _pick_recommend_tags(liked, wave, rng)
        found = self._sample_tags_parallel(tags, "new", rng)
        if len(found) < count and self._remaining() > 1.0:
            extra = _pick_recommend_tags(liked, count - len(found), rng)
            more = self._sample_tags_parallel(extra, "new", rng)
            seen = {post.post_id for post in found}
            for post in more:
                if post.post_id in seen:
                    continue
                found.append(post)
                if len(found) >= count:
                    break
        return found[:count]

    def _sample_from_tag(self, tag: str, count: int, sort: str, rng) -> list[Post]:
        """固定采样深度内并行多窗；不足时补首页。"""
        if sort not in SORT_TO_API:
            raise ApiError("排序只能是 hot、new 或 month。")
        name = tag.strip()
        if not name:
            raise ApiError("标签不能为空。")
        if count == 1:
            post = self._sample_one_from_tag(name, sort, rng)
            return [post] if post else []
        starts = _random_tag_starts(count, rng)
        pools = self._fetch_tag_windows_parallel(name, sort, starts)
        pool: list[Post] = []
        seen: set[str] = set()
        for batch in pools:
            for post in batch:
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                pool.append(post)
        if len(pool) < count and self._remaining() > 0.5:
            more = self._fetch_tag_window(name, sort, 0)
            for post in more:
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                pool.append(post)
        return _pick(pool, count, rng)

    def _sample_one_from_tag(self, tag: str, sort: str, rng) -> Post | None:
        """随机一窗；空则在预算内再试一次更浅的游标或首页。"""
        pages = max(1, TAG_DEPTH_DEFAULT // TAG_CURSOR_STEP)
        if self._remaining() < 0.3:
            return None
        start = rng.randrange(pages) * TAG_CURSOR_STEP
        pool = self._fetch_tag_window(tag, sort, start)
        if not pool and start != 0 and self._remaining() > 0.5:
            # 深窗常为空：再掷一次浅窗，仍不够则首页。
            shallow = max(1, pages // 4)
            start2 = rng.randrange(shallow) * TAG_CURSOR_STEP
            pool = self._fetch_tag_window(tag, sort, start2) if start2 else self._fetch_tag_window(tag, sort, 0)
            if not pool and start2 != 0 and self._remaining() > 0.5:
                pool = self._fetch_tag_window(tag, sort, 0)
        if not pool:
            return None
        picked = _pick(pool, 1, rng)
        return picked[0] if picked else None

    def _sample_tags_parallel(self, tags: list[str], sort: str, rng) -> list[Post]:
        if not tags:
            return []
        if len(tags) == 1 or self._transport is not None:
            found: list[Post] = []
            for name in tags:
                post = self._sample_one_from_tag(name, sort, rng)
                if post:
                    found.append(post)
            return found

        def job(name: str):
            child = self._child_client()
            with child:
                return child._sample_one_from_tag(name, sort, rng)

        found: list[Post] = []
        seen: set[str] = set()
        with ThreadPoolExecutor(max_workers=min(len(tags), MAX_PICKS)) as pool:
            futures = [pool.submit(job, name) for name in tags]
            for fut in as_completed(futures):
                try:
                    post = fut.result()
                except Exception:
                    continue
                if not post or post.post_id in seen:
                    continue
                seen.add(post.post_id)
                found.append(post)
        return found

    def _fetch_tag_windows_parallel(self, tag: str, sort: str, starts: list[int]) -> list[list[Post]]:
        if not starts:
            return []
        if len(starts) == 1 or self._transport is not None:
            return [self._fetch_tag_window(tag, sort, start) for start in starts]

        def job(start: int):
            child = self._child_client()
            with child:
                return child._fetch_tag_window(tag, sort, start)

        results: list[list[Post] | None] = [None] * len(starts)
        with ThreadPoolExecutor(max_workers=min(len(starts), 16)) as pool:
            futures = {pool.submit(job, start): index for index, start in enumerate(starts)}
            for fut in as_completed(futures):
                index = futures[fut]
                try:
                    results[index] = fut.result()
                except Exception:
                    results[index] = []
        return [batch or [] for batch in results]

    def _fetch_tag_window(self, tag: str, sort: str, start: int) -> list[Post]:
        return self.search_tag(
            tag, sort=sort, limit=SAMPLE_POOL, offset=0, max_pages=1, start_cursor=start
        )

    def _child_client(self) -> LofterClient:
        child = LofterClient(
            self._cookie,
            transport=self._transport,
            min_interval=self._min_interval,
            max_attempts=self._max_attempts,
            budget=max(0.5, self._remaining()),
        )
        # 同进程内复用已解析的域名 / 喜欢标签，仍不写磁盘。
        child._domain = self._domain
        child._favorite_tags = self._favorite_tags
        return child

    def get_favorite_tags(self) -> list[str]:
        if self._favorite_tags is not None:
            return list(self._favorite_tags)
        payload = self._request("GET", URL_ACCOUNT, params={"product": PRODUCT})
        tags = _tags_from_account(payload)
        self._favorite_tags = tags
        # 顺带记下域名，省掉 history/mine 再打一次账号接口。
        if self._domain is None:
            domain = _domain_from_account(payload)
            if domain:
                self._domain = domain
        return list(tags)

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
        if self._domain:
            return self._domain
        payload = self._request("GET", URL_ACCOUNT, params={"product": PRODUCT})
        tags = _tags_from_account(payload)
        if tags and self._favorite_tags is None:
            self._favorite_tags = tags
        domain = _domain_from_account(payload)
        if domain:
            self._domain = domain
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
            left = self._remaining()
            if left < 0.05:
                raise ApiError("请求超时，请稍后再试。")
            self._pace()
            request = HTTPRequest(method, url, params or {}, data=data, content=content, headers=merged)
            timeout = min(TIMEOUT_SECONDS, max(0.5, left))
            try:
                if self._transport:
                    response = self._transport(request)
                else:
                    response = _urllib_send(request, timeout=timeout)
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


def get_following_blog_posts(cookie: str, blog_key: str, limit: int = 20, offset: int = 0) -> list[Post]:
    return _run(cookie, "get_following_blog_posts", blog_key, limit, offset)


def sample_tag(cookie: str, tag: str, count: int = 1, sort: str = "hot") -> list[Post]:
    return _run(cookie, "sample_tag", tag, count, sort)


def recommend(cookie: str, count: int) -> list[Post]:
    return _run(cookie, "recommend", count)


def recommend_tag_candidates(liked, hot, rng) -> list[str]:
    """喜欢的标签优先，再热门，再内置兜底。打乱同组顺序后整表返回。"""
    liked_tags = _unique_tags(liked)
    liked_set = set(liked_tags)
    hot_tags = [tag for tag in _unique_tags(hot) if tag not in liked_set]
    used = liked_set | set(hot_tags)
    fallback = [tag for tag in FALLBACK_HOT_TAGS if tag not in used]
    rng.shuffle(liked_tags)
    rng.shuffle(hot_tags)
    rng.shuffle(fallback)
    chosen = liked_tags + hot_tags + fallback
    if not chosen:
        raise ApiError("没有可用的标签，暂时不能推荐。")
    return chosen


def select_recommend_tags(count: int, liked, hot, rng) -> list[str]:
    if count < 1:
        raise ApiError("篇数至少为 1。")
    return recommend_tag_candidates(liked, hot, rng)[:count]


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


_OPENER = urllib.request.build_opener()


def _urllib_send(request: HTTPRequest, timeout: float = TIMEOUT_SECONDS) -> HTTPResponse:
    headers = dict(request.headers)
    payload = request.content
    if request.data is not None and payload is None:
        payload = urllib.parse.urlencode(request.data).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    outgoing = urllib.request.Request(request.url.full, data=payload, headers=headers, method=request.method)
    try:
        with _OPENER.open(outgoing, timeout=timeout) as response:
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


def _take_pages(fetch, limit: int, offset: int, page_cap: int = PAGE_CAP) -> list:
    rows: list = []
    seen: set[str] = set()
    cursor = offset
    for _ in range(40):
        if len(rows) >= limit:
            break
        size = min(page_cap, max(limit - len(rows), 1))
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
            if isinstance(total, int) and cursor >= total:
                break
            continue
        if isinstance(nxt, int) and nxt <= cursor:
            break
        cursor += len(batch)
        if isinstance(total, int) and cursor >= total:
            break
        if len(batch) < size:
            break
    return rows[:limit]


def _take_novels(
    fetch,
    limit: int,
    skip: int,
    page_cap: int = PAGE_CAP,
    max_pages: int | None = None,
    max_empty_novel_pages: int | None = None,
    start_cursor: int = 0,
) -> list:
    kept: list = []
    seen: set[str] = set()
    cursor = start_cursor
    target = skip + limit
    empty_novel_pages = 0
    page_budget = 60 if max_pages is None else max(1, max_pages)
    for _ in range(page_budget):
        if len(kept) >= target:
            break
        batch, nxt, total = fetch(cursor, page_cap)
        if not batch:
            break
        fresh = 0
        novel_hits = 0
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
            novel_hits += 1
        if fresh == 0:
            break
        if novel_hits == 0:
            empty_novel_pages += 1
            if max_empty_novel_pages is not None and empty_novel_pages >= max_empty_novel_pages:
                break
        else:
            empty_novel_pages = 0
        # 有明确下一页游标就继续，绝不能因「本页条数 < page_cap」误停。
        if isinstance(nxt, int) and nxt > cursor:
            cursor = nxt
            if isinstance(total, int) and cursor >= total:
                break
            continue
        if isinstance(nxt, int) and nxt <= cursor:
            break
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


def _pick(pool: list[Post], count: int, rng) -> list[Post]:
    if count <= 0 or not pool:
        return []
    if len(pool) <= count:
        picked = list(pool)
        rng.shuffle(picked)
        return picked
    return rng.sample(pool, count)


def _pick_recommend_tags(liked: list[str], count: int, rng) -> list[str]:
    names = list(liked)
    if not names:
        return []
    if len(names) >= count:
        return rng.sample(names, count)
    picked = list(names)
    rng.shuffle(picked)
    while len(picked) < count:
        picked.append(names[rng.randrange(len(names))])
    return picked


def _domain_from_account(payload: dict) -> str | None:
    data = _expect_response(payload) if isinstance(payload, dict) else {}
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
    return None


def _random_tag_starts(count: int, rng) -> list[int]:
    pages = max(1, TAG_DEPTH_DEFAULT // TAG_CURSOR_STEP)
    starts: list[int] = []
    seen: set[int] = set()
    for _ in range(count * 3):
        if len(starts) >= count:
            break
        start = rng.randrange(pages) * TAG_CURSOR_STEP
        if start in seen:
            continue
        seen.add(start)
        starts.append(start)
    while len(starts) < count:
        starts.append(0)
    return starts


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
