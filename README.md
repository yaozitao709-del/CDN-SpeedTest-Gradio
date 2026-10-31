# CDN-SpeedTest-Gradio

给 [XIU2/CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 加一个本地 Gradio 界面：点击按钮测速、查看进度、停止任务、排序结果、复制推荐 IP 和回看历史。

**测速由运行程序的这台电脑发起。** 网页是操作界面，不会改为云服务器代测。

## 下载与启动

从 [Releases](https://github.com/yaozitao709-del/CDN-SpeedTest-Gradio/releases/latest) 下载 `CDN-SpeedTest-Gradio.zip`，完整解压。需要 **Python 3.11 或更新版本**。

| 系统 | 启动方式 | 随包核心 |
| --- | --- | --- |
| macOS Apple Silicon / Intel | 双击 `启动测速.command` | 官方 arm64 / amd64 |
| Windows x64 | 双击 `启动测速.cmd` | 官方 amd64 EXE |

首次启动会创建专用 `.venv` 并安装依赖，需要联网；后续直接打开本机网页。Windows 安装 Python 时请勾选 **Add Python to PATH**。

程序默认使用 `http://127.0.0.1:7861`，端口占用时自动选择 7862–7890。重复启动会打开已有实例，不会再开一个测速服务。默认只监听本机，不创建公网分享。

Release 工具包也提供“打开CDN测速网页”和“关闭CDN测速网页”快捷文件。关闭浏览器不会退出后台服务；也可在项目目录运行：

```bash
python launcher.py --stop
```

macOS 若解压后脚本缺少执行权限，可在项目目录执行：

```bash
chmod +x 启动测速.command
python3 launcher.py
```

## 功能

- 开始、停止和两阶段进度，不显示刷屏的终端进度条。
- 按下载速度或延迟排序，已完成下载验证的记录优先显示。
- 推荐 IP 一键复制；CSV 和原始日志直接下载。
- 每轮自动落盘，支持回看新旧终端版历史。
- 机房代码附中文城市，例如 `LAX · 美国洛杉矶`、`SEA · 美国西雅图`。
- 窗口宽度自适应；窄窗口下表格在自身区域横向滚动，表头和数据列保持对齐。
- 停止、失败或没有正下载结果时不沿用旧推荐。

## 如何使用

1. 在实际要使用的网络下退出系统代理、TUN 和 VPN。
2. 点击“开始测速”，等待连接测试和下载测速完成。
3. 复制推荐 IP，在自己的 CDN 节点中验证实际使用效果。
4. 需要更换参数时展开“测速设置”；历史和导出在页面内查看。

默认参数沿用原工具：100 并发、每个 IP 测 3 次连接、5 个下载候选、每个下载 5 秒、443 端口。下载测速会消耗流量。

默认候选来自 `ip.txt`，只覆盖其中的网段抽样，不保证找到全部 Cloudflare IP 中的最快地址。可以替换 `ip.txt`，每行一个 IPv4 地址或 IPv4 网段。

## 结果怎么看

| 字段 | 含义 |
| --- | --- |
| 延迟 ms | CFST 测得的 TCP 连接平均延迟 |
| 下载 MB/s | 下载速度，乘以 8 约等于 Mbps |
| 连接失败率 | 该轮 TCP 连接尝试的失败比例，不是实际网络丢包率 |
| CDN 机房 | 当次下载命中的入口地区，不是中转服务器所在地 |
| 未完成下载验证 | 可能未进入下载测试，也可能下载失败；不参与推荐 |

表格排序只改变展示顺序；推荐 IP 始终按已验证记录的下载速度选择。未收录的机房代码保留原码，不推测城市。原始 CSV 不作改写。

## 文件

- `app.py` / `style.css`：Gradio 界面。
- `runner.py`：官方核心进程、停止、进度和运行元数据。
- `results.py` / `locations.py`：结果解析、排序、历史与城市备注。
- `launcher.py`：环境准备、端口选择和单实例启动。
- `bin/`：官方测速核心；来源和 SHA256 见 `software-manifest.json`。
- `results/`：本机每轮 CSV、日志、元数据，默认不上传 Git。
- `data/`：本机服务日志和实例信息，默认不上传 Git。

完整操作说明见 [使用说明.md](使用说明.md)。

## 开发与验证

```bash
python -m venv .venv
# macOS
source .venv/bin/activate
# Windows 使用 .venv\Scripts\activate
python -m pip install -r requirements.lock.txt
python -m unittest discover -s tests -v
python launcher.py
```

当前有 23 项本地测试，覆盖结果解析、历史路径边界、取消与重启、损坏 CSV、旧推荐清理、启动信息写入失败的进程回收等。

macOS 已完成真实测速、开始/停止、排序、复制、下载、历史和 320–1920 px 布局验证。**Windows 启动器和官方核心已提供，尚未进行 Windows 实机验收。** 当前未提供 Linux 测速核心。

## 边界

程序不修改你的节点、订阅或服务器，也不会自动关闭系统代理。通过其他设备访问界面，测速仍发生在运行程序的电脑上。CDN 入口测速成功不保证实际代理节点可用，仍需在具体节点中验证。

## 来源与许可

测速核心来自 **XIU2/CloudflareSpeedTest v2.3.5**，随包二进制未经修改。感谢原作者。

- [原项目](https://github.com/XIU2/CloudflareSpeedTest)
- [官方 v2.3.5](https://github.com/XIU2/CloudflareSpeedTest/releases/tag/v2.3.5)
- [随附的对应版本源码](third_party/CloudflareSpeedTest-v2.3.5-source.tar.gz)
- [第三方说明](third_party/README.md)

项目以 [GPL-3.0](LICENSE) 分发。原测速核心的许可文本另见 `LICENSE-CloudflareSpeedTest.txt`。
