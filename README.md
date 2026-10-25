# lofter-cli / lftcli

在终端里读 LOFTER 的文字小说。

```bash
git clone https://github.com/ftmlycH/lftcli.git
cd lftcli
```

还没装进环境时，在项目目录运行 `python3 main.py`。装好后执行 `lftcli`，进入 `lofter>`。

## 安装

先装 Python 3.10+，再在项目目录执行 `python3 -m pip install -e .`。

| 系统 | 准备 Python |
| --- | --- |
| Windows | [python.org](https://www.python.org/downloads/windows/)，勾选 Add python.exe to PATH。没有 `python3` 时用 `py -3` |
| macOS | `brew install python`，或使用系统 / python.org 的 `python3` |
| Linux | Debian、Ubuntu 用下方 deb。Fedora、RHEL 用下方 rpm。 |
| FreeBSD、DragonFly | `pkg install python3` |
| OpenBSD | `pkg_add python3` |
| NetBSD | `pkgin install python312`，之后用 `python3.12` |

Debian、Ubuntu系可以用deb包下载

```bash
python3 packaging/build_deb.py
sudo python3 dist/install_deb.py dist/lftcli_0.1.0_all.deb
```

Fedora系可以用rpm包下载

```bash
python3 packaging/build_rpm.py
sudo rpm -i --nosignature dist/lftcli-0.1.0-1.noarch.rpm
```

配置文件：Windows 是 `%USERPROFILE%\.lofter-cli\config.json`，其他系统是 `~/.lofter-cli/config.json`。

## Cookie

用浏览器登录 [lofter.com](https://www.lofter.com/)，在开发者工具的 Cookies 里复制 `LOFTER-PHONE-LOGIN-AUTH`，没有就复制 `LOFTER_SESS`。然后执行 `cookie set`。Windows 上粘贴内容会显示出来；其他系统不回显。

`cookie` 只显示是否保存和长度，不显示内容。环境变量 `LOFTER_COOKIE` 优先于配置文件。

## 命令

在 `lofter>` 里直接输入，不要加 `lftcli`。`exit` 退出。列表只保留文字小说。

| 命令 | 作用 |
| --- | --- |
| `cookie` | 查看、保存或删除 Cookie |
| `collections [id]` | 订阅的合集。带 ID 则列出其中的文章，默认从旧到新 |
| `mine` | 我发布的文字 |
| `following` | 关注的人发布的文字 |
| `likes` | 我喜欢的文章 |
| `history` | 浏览过的文章 |
| `tag <tag> [--sort hot\|new\|month]` | 标签检索。`hot` 总热度，`new` 最新，`month` 月榜 |
| `random <tag> [--count N]` | 随机抽 N 篇，默认 1，最多 5 |
| `recommend <N>` | 推荐 N 篇，1 到 5 |
| `post <id 或链接>` | 读正文。`--markdown` 才带轻量标记 |
| `clear` / `cls` | 清屏 |

## 说明

地址和参数只在 `src/lftcli/client.py`。超时 12 秒，相邻请求至少间隔 0.1 秒，连接失败、超时和 HTTP 429 / 5xx 最多试 3 次。接口不是官方的，付费或仅自己可见的正文可能拿不到。不支持图片。
