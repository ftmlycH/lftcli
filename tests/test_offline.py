from __future__ import annotations

import builtins
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lftcli import client, config, main, output
from lftcli.parser import html_to_markdown, html_to_text


def _item(post_id=10, blog_id=20, title="标题一", hot=3200, author="作者A", content="<p>正文</p>"):
    return {
        "post": {
            "id": post_id,
            "blogId": blog_id,
            "title": title,
            "hot": hot,
            "type": 1,
            "publishTime": 1_700_000_000_000,
            "permalink": "aa_bb",
            "blogPageUrl": "https://author.lofter.com/post/aa_bb",
            "content": content,
            "digest": "摘要不该盖过标题",
            "blogInfo": {"blogNickName": author, "blogName": "author"},
            "postCount": {"postHot": hot},
        }
    }


class ParserTests(unittest.TestCase):
    def test_html_to_text(self):
        html = (
            "<script>alert(1)</script><style>p{}</style>"
            "<p>第一段</p><p>第二段<br>折行</p>"
            "<p>   </p><img src='http://example.test/a.jpg'><p>第三段</p>"
        )
        self.assertEqual(html_to_text(html), "第一段\n第二段\n折行\n\n第三段")

    def test_empty_and_nbsp(self):
        self.assertEqual(html_to_text("  "), "")
        self.assertEqual(html_to_text("<p>甲&nbsp;乙</p>"), "甲 乙")

    def test_markdown_keeps_emphasis_and_drops_images(self):
        html = "<h1>章</h1><p><strong>重</strong> <a href='https://example.test'>链</a></p><img src='x'>"
        text = html_to_markdown(html)
        self.assertIn("# 章", text)
        self.assertIn("**重**", text)
        self.assertIn("[链](https://example.test)", text)
        self.assertNotIn("img", text)

    def test_deb_contains_command_and_dependencies(self):
        import gzip
        import importlib.util
        import io
        import tarfile

        script = Path(__file__).resolve().parents[1] / "packaging" / "build_deb.py"
        spec = importlib.util.spec_from_file_location("lftcli_build_deb", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lftcli_test.deb"
            module.build_deb(target)
            blob = target.read_bytes()
        self.assertTrue(blob.startswith(b"!<arch>\n"))
        members = _read_ar(blob)
        self.assertEqual(members["debian-binary"], b"2.0\n")
        control = _tar_names(members["control.tar.gz"])
        data = _tar_names(members["data.tar.gz"])
        self.assertIn("./control", control)
        self.assertIn("./usr/bin/lftcli", data)
        self.assertIn("./usr/share/lftcli/__init__.py", data)
        self.assertIn("./usr/share/lftcli/main.py", data)
        self.assertIn("from lftcli.main import main", _tar_text(members["data.tar.gz"], "./usr/bin/lftcli"))
        control_text = _tar_text(members["control.tar.gz"], "./control")
        self.assertIn("Package: lftcli", control_text)
        self.assertIn("Depends: python3\n", control_text)
        self.assertNotIn("python3-httpx", control_text)
        self.assertNotIn("python3-bs4", control_text)
        self.assertNotIn("./usr/share/lftcli/vendor/httpx/__init__.py", data)

    def test_cjk_wrap_uses_double_width(self):
        self.assertEqual(output._wrap_line("甲" * 4, 4), ["甲甲", "甲甲"])
        self.assertEqual(output._wrap_line("abcd", 4), ["abcd"])

    def test_div_becomes_newline(self):
        self.assertEqual(html_to_text("<div>甲</div><div>乙</div>"), "甲\n乙")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("LOFTER_COOKIE", None)
        self._config_path = config.CONFIG_PATH
        self._index_path = config.INDEX_PATH

    def tearDown(self):
        config.CONFIG_PATH = self._config_path
        config.INDEX_PATH = self._index_path
        if self._env is None:
            os.environ.pop("LOFTER_COOKIE", None)
        else:
            os.environ["LOFTER_COOKIE"] = self._env

    def test_repl_exits_without_network(self):
        answers = iter(["exit"])

        def fake_input(_prompt: str = "") -> str:
            return next(answers)

        original = builtins.input
        builtins.input = fake_input
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main.main([])
        finally:
            builtins.input = original
        self.assertEqual(code, 0)
        self.assertIn("exit", stdout.getvalue())

    def test_clear_screen_does_not_need_cookie_or_network(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("不应发起网络请求")

        original = client.get_collections
        client.get_collections = boom
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main.main(["clear"])
        finally:
            client.get_collections = original
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_missing_cookie_does_not_call_network(self):
        config.CONFIG_PATH = Path(tempfile.gettempdir()) / "lofter-cli-missing-config.json"

        def boom(*_args, **_kwargs):
            raise AssertionError("不应发起网络请求")

        original = client.get_collections
        client.get_collections = boom
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = main.main(["collections"])
        finally:
            client.get_collections = original
        self.assertEqual(code, 1)
        self.assertIn("不会发起网络请求", buf.getvalue())
        self.assertIn("cookie set", buf.getvalue())

    def test_cookie_set_hides_secret_and_skips_network(self):
        secret = "phone-token-0123456789"
        with tempfile.TemporaryDirectory() as directory:
            config.CONFIG_PATH = Path(directory) / "config.json"
            config.CONFIG_PATH.write_text('{"keep": true}\n', encoding="utf-8")

            def boom(*_args, **_kwargs):
                raise AssertionError("不应发起网络请求")

            original = client.check_login
            client.check_login = boom
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(f'Cookie: "{secret}"\n')
            try:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main.main(["cookie", "set", "--no-check"])
                    status = main.main(["cookie"])
            finally:
                sys.stdin = old_stdin
                client.check_login = original
            shown = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(code, 0)
            self.assertEqual(status, 0)
            self.assertNotIn(secret, shown)
            self.assertIn("长度", shown)
            self.assertIn("当前生效：配置文件", shown)
            saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookie"], secret)
            self.assertTrue(saved["keep"])

    def test_cookie_set_keeps_file_when_login_is_rejected(self):
        secret = "rejected-token-012345"
        with tempfile.TemporaryDirectory() as directory:
            config.CONFIG_PATH = Path(directory) / "config.json"
            original = client.check_login
            client.check_login = lambda _cookie: (_ for _ in ()).throw(client.AuthError("拒绝"))
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(secret)
            try:
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    code = main.main(["cookie", "set"])
            finally:
                sys.stdin = old_stdin
                client.check_login = original
            self.assertEqual(code, 1)
            self.assertIn("已保存", stderr.getvalue())
            self.assertNotIn(secret, stderr.getvalue())
            self.assertEqual(config.load_saved_cookie(), secret)

    def test_environment_cookie_overrides_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config.CONFIG_PATH = Path(directory) / "config.json"
            config.save_cookie("file-token-abcdefgh")
            os.environ[config.ENV_COOKIE] = "env-token-abcdefgh"
            self.assertEqual(config.load_cookie(), "env-token-abcdefgh")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main.main(["cookie", "status"])
            self.assertEqual(code, 0)
            text = stdout.getvalue()
            self.assertIn("当前生效：环境变量", text)
            self.assertNotIn("env-token-abcdefgh", text)
            self.assertNotIn("file-token-abcdefgh", text)

    def test_clear_cookie_leaves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config.CONFIG_PATH = Path(directory) / "config.json"
            config.save_cookie("file-token-abcdefgh")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main.main(["cookie", "clear"])
            self.assertEqual(code, 0)
            self.assertIsNone(config.load_saved_cookie())
            self.assertIn("已从", stdout.getvalue())

    def test_trailing_quote_is_removed_from_cookie(self):
        self.assertEqual(config.normalize_cookie("abcdefgh'"), "abcdefgh")
        self.assertEqual(config.normalize_cookie("'abcdefgh'"), "abcdefgh")

    def test_control_character_in_cookie_is_rejected(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.normalize_cookie("abcdefgh\x16ijklmnop")
        self.assertIn("控制字符", str(caught.exception))

    def test_corrupt_index_is_kept_aside(self):
        with tempfile.TemporaryDirectory() as directory:
            config.INDEX_PATH = Path(directory) / "posts.json"
            config.INDEX_PATH.write_text("{", encoding="utf-8")
            config.remember_posts([SimpleNamespace(post_id="7", blog_id="8", url="https://a.lofter.com/post/x")])
            self.assertEqual(config.lookup_post("7")["blog_id"], "8")
            self.assertEqual((config.INDEX_PATH.parent / "posts.json.bak").read_text(encoding="utf-8"), "{")


class ClientTests(unittest.TestCase):
    def test_control_character_is_not_sent_as_cookie_header(self):
        headers = client._headers("abcdefgh\x16ijklmnop")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("lofter-phone-login-auth", headers)

    def test_recommend_prefers_liked_tags_then_hot_tags(self):
        import random as random_module

        rng = random_module.Random(1)
        tags = client.select_recommend_tags(2, ["甲", "乙", "甲"], ["热门"], rng)
        self.assertEqual(len(tags), 2)
        self.assertTrue(set(tags) <= {"甲", "乙"})

        rng = random_module.Random(2)
        tags = client.select_recommend_tags(3, ["甲"], ["热1", "热2", "甲"], rng)
        self.assertEqual(tags[0], "甲")
        self.assertEqual(sorted(tags[1:]), ["热1", "热2"])

        rng = random_module.Random(3)
        tags = client.select_recommend_tags(5, ["甲"], ["热1"], rng)
        self.assertEqual(tags, ["甲", "热1"])

        with self.assertRaises(client.ApiError):
            client.select_recommend_tags(1, ["", "   "], [], random_module.Random(0))

    def test_account_tags_ignore_unrelated_fields(self):
        payload = {
            "response": {
                "manageTags": [{"tagName": "原创小说"}, "无CP", {"phone": "13800000000"}],
                "blogs": [{"blogInfo": {"keyTag": "短篇"}}],
            }
        }
        self.assertEqual(client._tags_from_account(payload), ["原创小说", "无CP", "短篇"])

    def test_fav_parser_reads_liked_text_post(self):
        text = (
            "var s1={};var s2={};"
            's1.id=10;s1.blogId=20;s1.type=1;s1.title="标题";s1.hot=8;'
            's1.blogPageUrl="https://alpha.lofter.com/post/aa_bb";'
            "s1.blogInfo=s2;s2.blogNickName=\"作者\";"
            "s1.phone=\"13800000000\";"
            "dwr.engine._remoteHandleCallback('1','0',[s1]);"
        )
        posts = client._parse_fav_posts(text)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].post_id, "10")
        self.assertEqual(posts[0].author, "作者")
        self.assertEqual(posts[0].hot, 8)
        self.assertNotIn("138", posts[0].title)

    def test_dwr_unicode_escapes_become_chinese(self):
        text = (
            "s1.id=10;s1.blogId=20;s1.type=1;"
            r's1.title="\u6807\u9898";'
            "s1.blogInfo=s2;"
            r's2.blogNickName="\u4f5c\u8005";'
            "dwr.engine._remoteHandleCallback('1','0',[s1]);"
        )
        posts = client._parse_fav_posts(text)
        self.assertEqual(posts[0].title, "标题")
        self.assertEqual(posts[0].author, "作者")
        self.assertEqual(html_to_text(client._article_html("&lt;p&gt;正文&lt;/p&gt;")), "正文")

    def test_random_page_does_not_start_at_the_first_hot_page(self):
        seen = {}

        def handler(request: client.HTTPRequest) -> client.HTTPResponse:
            seen["offset"] = request.url.params["offset"]
            items = [_item(post_id=i + 24, title=f"标题{i}") for i in range(12)]
            return client.HTTPResponse(
                200,
                json={"meta": {"status": 200, "msg": ""}, "response": {"offset": 36, "items": items}},
            )

        class Rng:
            def randrange(self, _stop):
                return 2

            def shuffle(self, _items):
                return None

            def sample(self, population, count):
                return list(population)[:count]

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            posts = api.sample_tag("原创小说", 1, rng=Rng())
        self.assertEqual(seen["offset"], "24")
        self.assertEqual(posts[0].post_id, "24")

    def test_following_parser_ignores_extra_fields(self):
        text = (
            's0.blogName="alpha";\n'
            's0.blogNickName="作者";\n'
            's0.blogId=12;\n'
            's0.homePageUrl="https://alpha.lofter.com";\n'
            's0.phone="13800000000";\n'
        )
        blogs = client._parse_following(text)
        self.assertEqual(blogs[0].name, "alpha")
        self.assertEqual(blogs[0].nickname, "作者")
        self.assertEqual(blogs[0].blog_id, "12")
        self.assertNotIn("13800000000", blogs[0].nickname)

    def test_permalink_becomes_decimal_ids(self):
        blog_id, post_id = client._resolve_post(
            "https://in-me.lofter.com/post/3102d837_1c8eb2b8b"
        )
        self.assertEqual(blog_id, "822270007")
        self.assertEqual(post_id, "7665822603")

    def test_hot_sort_uses_total_and_slices_offset(self):
        seen = {}

        def handler(request: client.HTTPRequest) -> client.HTTPResponse:
            seen["type"] = request.url.params["type"]
            seen["offset"] = request.url.params["offset"]
            items = [_item(post_id=i, title=f"标题{i}", hot=100 - i) for i in range(12)]
            return client.HTTPResponse(
                200,
                json={"meta": {"status": 200, "msg": ""}, "response": {"offset": 12, "items": items}},
            )

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            posts = api.search_tag("原创小说", sort="hot", limit=2, offset=1)
        self.assertEqual(seen["type"], "total")
        self.assertEqual([post.post_id for post in posts], ["1", "2"])
        self.assertEqual(posts[0].author, "作者A")
        self.assertEqual(posts[0].hot, 99)

    def test_retries_server_error_then_succeeds(self):
        calls = {"n": 0}

        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            calls["n"] += 1
            if calls["n"] < 3:
                return client.HTTPResponse(503, text="busy")
            return client.HTTPResponse(
                200,
                json={
                    "code": 200,
                    "data": {
                        "offset": 1,
                        "subscribeCollectionCount": 1,
                        "collections": [
                            {
                                "collectionId": 9,
                                "name": "合集甲",
                                "blogNickName": "作者B",
                                "postCount": 4,
                                "valid": True,
                            }
                        ],
                    },
                },
            )

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            rows = api.get_collections(limit=10)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(rows[0].collection_id, "9")
        self.assertEqual(rows[0].post_count, 4)

    def test_auth_failure_is_not_retried(self):
        calls = {"n": 0}

        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            calls["n"] += 1
            return client.HTTPResponse(200, json={"msg": "请登录", "code": 401})

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            with self.assertRaises(client.AuthError) as caught:
                api.get_collections()
        self.assertEqual(calls["n"], 1)
        self.assertIn("LOFTER-PHONE-LOGIN-AUTH", str(caught.exception))

    def test_other_http_error_is_not_retried(self):
        calls = {"n": 0}

        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            calls["n"] += 1
            return client.HTTPResponse(404, text="missing")

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            with self.assertRaises(client.ApiError):
                api.get_likes()
        self.assertEqual(calls["n"], 1)

    def test_duplicate_page_does_not_repeat(self):
        calls = {"n": 0}
        row = {
            "collectionId": 9,
            "name": "合集甲",
            "blogNickName": "作者B",
            "postCount": 4,
            "valid": True,
        }

        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            calls["n"] += 1
            return client.HTTPResponse(
                200,
                json={
                    "code": 200,
                    "data": {
                        "offset": calls["n"] * 10,
                        "subscribeCollectionCount": 100,
                        "collections": [row] * 10,
                    },
                },
            )

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            rows = api.get_collections(limit=10)
        self.assertEqual(calls["n"], 2)
        self.assertEqual([item.collection_id for item in rows], ["9"])

    def test_tag_skips_image_posts(self):
        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            image = _item(post_id=1, title="图片帖")
            image["post"]["type"] = 2
            novel = _item(post_id=2, title="小说")
            return client.HTTPResponse(
                200,
                json={
                    "meta": {"status": 200, "msg": ""},
                    "response": {"offset": -1, "items": [image, novel]},
                },
            )

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            posts = api.search_tag("原创小说", limit=5)
        self.assertEqual([post.post_id for post in posts], ["2"])

    def test_one_bad_post_does_not_drop_the_page(self):
        def handler(_request: client.HTTPRequest) -> client.HTTPResponse:
            return client.HTTPResponse(
                200,
                json={
                    "meta": {"status": 200, "msg": ""},
                    "response": {"offset": -1, "items": [{"post": {"title": "坏了"}}, _item(post_id=5)]},
                },
            )

        with client.LofterClient("cookie", transport=handler, min_interval=0) as api:
            posts = api.search_tag("原创", limit=5)
        self.assertEqual([post.post_id for post in posts], ["5"])
        self.assertEqual(posts[0].content_html, "")

    def test_numeric_id_uses_local_index(self):
        original = config.INDEX_PATH
        config.INDEX_PATH = Path(tempfile.gettempdir()) / "lofter-cli-missing-index.json"
        try:
            with self.assertRaises(client.ApiError):
                client._resolve_post("123456")
        finally:
            config.INDEX_PATH = original


def _read_ar(blob: bytes) -> dict[str, bytes]:
    if not blob.startswith(b"!<arch>\n"):
        raise AssertionError("not a deb archive")
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


def _tar_names(blob: bytes) -> set[str]:
    import gzip
    import io
    import tarfile

    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as compressed:
        with tarfile.open(fileobj=compressed, mode="r:") as archive:
            return {item.name for item in archive}


def _tar_text(blob: bytes, name: str) -> str:
    import gzip
    import io
    import tarfile

    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as compressed:
        with tarfile.open(fileobj=compressed, mode="r:") as archive:
            member = archive.extractfile(name)
            if member is None:
                raise AssertionError(name)
            return member.read().decode("utf-8")


if __name__ == "__main__":
    unittest.main()
