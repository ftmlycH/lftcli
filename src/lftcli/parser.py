"""把文章 HTML 转成纯文本。这里不做任何网络请求，也不依赖第三方库。"""

from __future__ import annotations

from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "img"}
_BLOCK_TAGS = {"p", "div", "li", "blockquote", "section", "article", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


def html_to_text(html: str) -> str:
    """去掉 script/style，把 <br> 和 </p> 变成换行，合并连续空行，去掉首尾空白。"""
    return _render(html, markdown=False)


def html_to_markdown(html: str) -> str:
    """可选的轻量 Markdown。图片同样忽略。"""
    return _render(html, markdown=True)


def _render(html: str, markdown: bool) -> str:
    if html is None:
        return ""
    if not isinstance(html, str):
        raise TypeError("html 必须是字符串")
    if not html.strip():
        return ""
    parser = _ArticleParser(markdown)
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        blank = line.strip() == ""
        if blank:
            if previous_blank:
                continue
            collapsed.append("")
            previous_blank = True
            continue
        collapsed.append(line)
        previous_blank = False
    return "\n".join(collapsed).strip()


class _ArticleParser(HTMLParser):
    def __init__(self, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.parts: list[str] = []
        self._skip = 0
        self._link_href = ""
        self._link_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "img":
            return
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._write("\n")
            return
        if self.markdown and tag in {"h1", "h2", "h3"}:
            self._write("#" * int(tag[1]) + " ")
        elif self.markdown and tag in {"strong", "b"}:
            self._write("**")
        elif self.markdown and tag in {"em", "i"}:
            self._write("*")
        elif self.markdown and tag == "a":
            self._link_href = dict(attrs).get("href") or ""
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "img":
            return
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if self.markdown and tag == "a" and self._link_text is not None:
            label = "".join(self._link_text).strip()
            href = self._link_href.strip()
            self._link_text = None
            self._link_href = ""
            self.parts.append(f"[{label}]({href})" if href else label)
            return
        if self.markdown and tag in {"strong", "b"}:
            self._write("**")
        elif self.markdown and tag in {"em", "i"}:
            self._write("*")
        if tag in _BLOCK_TAGS:
            self._write("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._link_text is not None:
            self._link_text.append(data)
            return
        self.parts.append(data)

    def _write(self, text: str) -> None:
        if self._link_text is not None:
            self._link_text.append(text)
            return
        self.parts.append(text)
