
## 安装

需要 Python 3.10 或更高版本。

```bash
python -m pip install -e .
```

装好之后，在终端输入 `lftcli` 进入 `lofter>`，再输入指令。还没装进环境时，在项目目录里运行 `python main.py`，效果相同。

```text
lftcli
lofter> tag 原创小说 --sort hot --limit 5
lofter> post 123456
lofter> clear
lofter> exit
```

列表只保留文字小说。标记为图片或视频的帖子不会出现。

只需要 Python 3.10 或更高版本，没有第三方库。

## Debian / Ubuntu 安装包

在项目目录生成安装包。依赖已经放进包里，安装时不要用 `apt`，避免「读取软件包列表」时的段错误。

```bash
python3 packaging/build_deb.py
sudo dpkg -i dist/lftcli_0.1.0_all.deb
```

如果 `dpkg` 也报错，改用下面这条，它只解压文件，不读取软件包列表：

```bash
sudo python3 dist/install_deb.py dist/lftcli_0.1.0_all.deb
```

系统上需要已有 Python 3.10 或更高版本。装完后重新打开终端，执行 `lftcli`。

## Cookie

默认用浏览器里复制的 Cookie，不会自动登录，也不处理验证码。订阅合集、喜欢和浏览记录必须登录；标签和公开文章有时不校验 Cookie，但本程序在任何网络请求之前都会先确认 Cookie 已经配置。

1. 用浏览器登录 [lofter.com](https://www.lofter.com/)。
2. 打开开发者工具，进入 Application / 应用程序 → Cookies → `https://www.lofter.com`。
3. 复制 `LOFTER-PHONE-LOGIN-AUTH` 的值。手机号登录一般是这一项。没有的话，复制 `LOFTER_SESS`，或者整段 Cookie。
4. 在终端运行 `lofter cookie set`，按提示粘贴后回车。Windows 上这一步会显示在屏幕上，因为隐藏输入无法粘贴，还会把内容弄坏。

保存位置是 `%USERPROFILE%\.lofter-cli\config.json`（Linux / macOS 是 `~/.lofter-cli/config.json`）。`lofter cookie` 只显示是否已保存和长度，不显示内容。`lofter cookie clear` 删除文件里的 Cookie。`lofter cookie check` 向服务器确认当前生效的登录。

从管道读入也可以：`Get-Content cookie.txt | lofter cookie set`。保存后默认会请求一次账号接口确认登录；只要保存、不联网时加 `--no-check`。

环境变量 `LOFTER_COOKIE` 优先于配置文件。变量还在时，刚保存的文件不会被后续命令使用。不要把配置文件提交到仓库。

Cookie 失效时，程序会提示重新复制 `LOFTER-PHONE-LOGIN-AUTH` 或 `LOFTER_SESS`，不会把 traceback 打到终端。

## 命令

```text
lofter cookie
lofter cookie set [--no-check]
lofter cookie check
lofter cookie clear
lofter collections [--limit] [--offset]
lofter collection <合集ID> [--limit] [--offset] [--order old|new]
lofter likes [--limit] [--offset]
lofter history [--limit] [--offset]
lofter tag <标签> [--sort hot|new|month] [--limit] [--offset]
lofter random <标签> [--sort hot|new|month] [--count N] [--limit] [--offset]
lofter post <数字ID或文章链接> [--markdown]
lofter clear
```

| 命令 | 作用 |
| --- | --- |
| `cookie` | 查看、保存或删除本机 Cookie |
| `collections` | 当前账号订阅的合集 |
| `collection` | 合集里的文章。默认 `old`，从旧到新 |
| `mine` | 我发布的文字 |
| `following` | 我关注的人。看到名字后用 `blog 名字` 看对方的文字 |
| `blog` | 某个博客发布的文字 |
| `likes` | 我喜欢的文章。平台这个接口经常失败，失败时会说明原因 |
| `history` | 账号里浏览过的文章 |
| `tag` | 标签检索。`hot` 是总热度，`new` 是最新，`month` 是月榜 |
| `recommend 篇数` | 每个选中的标签随机一篇。先用喜欢的标签，不够再用热门标签 |
| `random` | 先按排序拉一批，再在本地随机抽 N 篇。不额外请求别的接口 |
| `post` | 一篇文章的纯文本。`--markdown` 才带轻量标记 |
| `clear` / `cls` / `清空` | 清空屏幕。Windows 和 Linux 都会处理 |

Windows 的传统控制台会打开 ANSI 显示，中文和分隔线才能正常出现。Linux 和 macOS 上方向键可以改正在输入的指令。正文会按终端宽度折行，一个汉字按两个宽度计算。配置文件在 Linux / macOS 上是 `~/.lofter-cli/config.json`，权限会设成只有当前用户可读。

文章列表固定为一行一篇：

```text
[7665822603] 标题 · 作者 · 热度 129170
```

合集列表：

```text
[5333228] 合集名 · 作者 · 12篇
```

文章：

```text
标题：标题
作者：作者
────────────────────────
正文第一段

正文第二段
```

`lofter post 数字ID` 能打开的，是之前在列表里出现过的文章。正文接口需要博客 ID 和文章 ID，列表命令会把这个对应关系记在 `~/.lofter-cli/posts.json`。手里只有链接时，直接把链接传给 `post`。

`random` 的 `--limit` 是候选池大小，不是最终篇数。不写时，候选池至少 24 条，且不少于 `--count`，最多 60 条。`--count` 最大 20。

标签接口每页条数由服务端决定（目前一页 12 条）。`search_tag` 会在内部用 offset 把页拼够，调用方只传 `tag`、`sort`、`limit`、`offset`。

## 请求约束

这些都封在 `client.py` 里：

- 超时 15 秒。
- 相邻请求至少间隔 0.3 秒。
- 连接失败、超时，以及 HTTP 429 / 5xx，最多请求 3 次。
- 其他 HTTP 错误不重试。
- HTTP 401 / 403，或响应体里的 401 / 403，变成登录失效提示。

阅读会走文章详情接口。平台可能把这次读取算进浏览量。

## 已知局限

- 接口不是官方的。失效时更新 `client.py` 顶部的地址和参数，命令和输出不用改。
- 订阅、喜欢、浏览记录需要有效登录 Cookie。付费或仅自己可见的正文，可能拿不到全文。
- 热度排序受平台过滤影响，不保证严格按数字递减。
- 不支持图片。纯文本模式下图片会被去掉；没有文字时会提示这篇没有可阅读的正文。
- 「我的喜欢」走网页收藏接口。如果返回结构变了，只改 `client.py`。
