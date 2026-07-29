# 极简多机进程监视器

Python 标准库后端、原生 HTML/CSS/JavaScript 前端、系统 SSH、零数据库、目标机零 Agent。

正式文档只有三份：

- 产品行为：[`doc/PRD.md`](doc/PRD.md)
- 技术实现：[`doc/TECHNICAL_DESIGN.md`](doc/TECHNICAL_DESIGN.md)
- 启动运维：本 README

## 极简原则

本项目用最少的组件完成 3–10 台 Linux 机器的日常进程观察与轻量管理：

- **零额外运行时依赖**：后端只使用 Python 标准库，前端只使用原生 HTML/CSS/JavaScript，采集复用系统已有的 `ssh`、`ps`、`df` 和 `du`。
- **零目标机 Agent**：目标机不安装服务、不开放新端口，只要求已有 SSH 可以免交互登录。
- **零数据库与历史堆积**：机器配置保存在一个 JSON 文件；快照、连通状态和目录结果只保存在内存，不记录历史时序数据。
- **按需采集**：页面只周期请求当前机器；同一机器的多个浏览器共享后端快照，默认每 5 秒最多实际采集一次，不因页面数量重复执行 SSH。
- **能力默认收敛**：终止进程和清空目录默认关闭，必须通过启动参数显式开启；目录只在点击时执行 `du`，结果缓存 10 分钟。
- **优先复用现有结构**：新增需求优先复用当前单进程、单接口和内存缓存；没有明确收益时，不引入框架、数据库、消息队列、常驻 Agent 或额外部署层。

当前仓库包含 Git 历史约 1.3 MiB，实际受 Git 跟踪的文件约 0.22 MiB。两台机器各保留一份约 100 KB 的进程快照时，`monitor.py` 实测常驻 RSS 约 29 MB、空闲 CPU 接近 0%；增加到 10 台机器通常仍只需约 30–35 MB 常驻内存。SSH 健康检查和资源采集会短暂启动子进程，但不会常驻。目录 `du` 是唯一可能产生明显磁盘 I/O 的操作，因此严格保持按点击执行和 10 分钟缓存。

## 1. 快速体验

```bash
cd /home/ubuntu/Developer/monitor
python3 monitor.py --demo --allow-kill --allow-delete 3
```

浏览器访问：

```text
http://127.0.0.1:8080
```

演示模式提供四台模拟机器、进程变化、硬盘目录树、终止与清空操作，不访问真实服务器。

如果只想查看页面，不启动 Python，可以直接用浏览器打开 [`doc/index.html`](doc/index.html)。页面检测到 `file://` 后会自动启用内置模拟数据，提供三台机器、动态进程、磁盘目录树及常用交互；这些变化只保存在当前浏览器页面内。通过 HTTP 打开时不会启用这套数据。

## 2. 监控本机

只读启动：

```bash
python3 monitor.py
```

允许终止进程，并从第 3 级开始清空目录内部内容：

```bash
python3 monitor.py --allow-kill --allow-delete 3
```

默认监听：

```text
127.0.0.1:8080
```

## 3. 通过局域网或公网 IP 直接访问

监听非本机地址时必须配置 Web 登录：

```bash
MONITOR_USERNAME=admin \
MONITOR_PASSWORD='替换为强密码' \
python3 monitor.py \
  --allow-kill \
  --allow-delete 3 \
  --bind=0.0.0.0 \
  --port=8080
```

访问：

```text
http://服务器IP:8080
```

浏览器会显示 HTTP Basic Authentication 登录框。用户名和密码由 `monitor.py` 校验，不写入 `hosts.json`。

如果浏览器一直转圈，可能是 Basic Authentication 登录框被遮挡。可以直接使用以下地址登录：

```text
http://{account}:{passwd}@{IP}:8080/
```

请将占位符替换为实际账号、密码和服务器 IP；密码包含 `@`、`:`、`/` 等特殊字符时需要先进行 URL 编码。该形式可能把凭据留在浏览器历史记录中，只建议用于临时排查，使用后应清理记录。

程序自身只提供 HTTP。直接开放公网可以访问，但明文 HTTP 不能保护登录凭据和危险操作；长期公网使用应配置 HTTPS。

## 4. 通过域名访问

推荐让 `monitor.py` 只监听回环地址：

```bash
MONITOR_USERNAME=admin \
MONITOR_PASSWORD='替换为强密码' \
python3 monitor.py \
  --allow-kill \
  --allow-delete 3 \
  --bind=127.0.0.1 \
  --port=9500
```

再由 Caddy、Nginx 或可信 VPN 将域名流量转发到：

```text
http://127.0.0.1:9500
```

反向代理负责域名和 HTTPS，`monitor.py` 负责页面、API 与 Basic Authentication。

## 5. 添加远程机器

在页面顶部机器 Tab 的 `＋` 中填写：

- 机器名称
- IP 地址或主机名
- SSH 用户
- SSH 端口

“测试连接”只验证，不保存；“保存”会在最终验证成功后写入当前使用的机器配置文件。

运行 `monitor.py` 的系统账户必须可以免交互登录目标机：

```bash
ssh -o BatchMode=yes -p 22 user@server true
```

网页不接收 SSH 密码或私钥。请提前配置 SSH Key 和目标机的 `authorized_keys`。

仓库中的 `hosts.json` 是不含真实服务器信息的安全默认配置。首次部署建议复制为只在本机保存、已被 Git 忽略的配置：

```bash
cp hosts.json hosts.local.json
# 编辑 hosts.local.json 后启动
python3 monitor.py
```

程序会优先读取同目录 `hosts.local.json`，不存在时再读取提交到 Git 的 `hosts.json`。也可以用 `--hosts` 显式指定其他路径。

机器配置修改后无需重启；`monitor.py` 自身代码修改后需要重启服务。

## 6. 反向 SSH 隧道

如果远程机器通过反向隧道映射到监控机回环地址，例如：

```text
127.0.0.1:2222
```

机器配置应保留真实隧道入口：

```json
{
  "name": "隧道服务器",
  "address": "127.0.0.1",
  "user": "your-user",
  "port": 2222,
  "local": false
}
```

程序只对“非本机 + 回环地址”尝试识别隧道公网对端。识别使用：

```bash
sudo -n ss -H -ltnp
sudo -n ss -H -tnp state established
```

运行账户需要免密执行上述 `ss` 命令。识别结果缓存 10 分钟；失败时页面显示 `127.0.0.1:2222`，不会影响普通 SSH 地址。

## 7. 采集与缓存行为

- 页面只周期采集当前选中的机器。
- 后端每 30 秒并行执行一次轻量 SSH 健康检查；这不会采集其他机器的 CPU、内存、硬盘或进程。
- 每个机器 Tab 的点表示 SSH 连通性：成功为绿，首次检测或首次失败复查为黄，连续两次失败为红。
- 首次失败约 3 秒后复查；状态超过 75 秒未更新时显示黄色。
- 浏览器每 30 秒读取 `/api/host-statuses` 内存缓存，不直接执行或触发 SSH。
- CPU/内存采集时间前的点只表示当前机器资源采集结果，与 Tab 点逻辑独立。
- CPU/内存默认每 5 秒请求一次当前机器；周期来自后端 `--interval`。
- 后端按机器共享最新快照：同一周期内多个浏览器直接读取内存，快照过期后的同机并发请求只执行一次 SSH/本机采集。
- 10 个页面同时查看同一台机器时，默认仍最多每 5 秒实际采集一次；查看不同机器时各机器独立采集。
- 切走后停止采集旧机器，但保留它的最后快照。
- 切回来先立即展示缓存，再刷新当前机器。
- 硬盘页面没有 5 秒刷新，只在点击目录时查询。
- 后端目录结果缓存 10 分钟；浏览器已经展开的数据继续保留。
- 服务重启后快照缓存清空，当前使用的机器配置文件保留。

采集周期可调整，最小 1 秒：

```bash
python3 monitor.py --interval=5
```

## 8. 功能开关

| 参数 | 默认 | 作用 |
|---|---|---|
| `--allow-kill` | 关闭 | 允许从 CPU/内存页面发送 SIGTERM |
| `--allow-delete N` | 关闭 | 允许清空挂载点下第 N 级及更深目录的内部内容；N ≥ 3，目标目录保留 |
| `--demo` | 关闭 | 使用模拟数据 |
| `--bind` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `8080` | HTTP 端口，合法范围 1–65535 |
| `--interval` | `5` | 浏览器请求周期及后端每机共享快照有效期，最小 1 秒 |
| `--hosts` | 优先 `hosts.local.json`，否则 `hosts.json` | 显式指定机器配置文件 |
| `--username` | 环境变量 | Web Basic Auth 用户名 |
| `--password` | 环境变量 | Web Basic Auth 密码 |

环境变量名称：

```text
MONITOR_USERNAME
MONITOR_PASSWORD
```

## 9. 常见问题

### 端口报 `OverflowError`

端口必须在 1–65535 之间：

```bash
python3 monitor.py --port=9500
```

不要把多个数字或其他参数误写到 `--port` 后面。

### 页面修改后没有变化

服务不支持代码热更新。停止旧进程后重新启动，并刷新浏览器：

```bash
ss -ltnp | grep ':8080'
```

### SSH 测试通过但保存失败

检查机器名称、地址、用户和端口是否仍是测试时的值。保存会再次执行连接验证。

### 目录统计很慢

目录占用使用 `du` 递归读取文件元数据。首次扫描大目录或大量小文件可能较慢；完成后同一路径缓存 10 分钟。远程扫描会跳过 `/proc`、`/dev`、`/sys` 等属于其他设备的直接子挂载点。

### `Permission denied`

目录读取、终止进程和清空目录都使用本机运行账户或远程 SSH 用户权限。部分子目录不可读时，页面保留可读结果，并显示具体路径，例如 `无法读取 /home/lighthouse：Permission denied`；目标目录本身不可读时则停止该次统计。请只授予实际需要的最小权限。

若页面显示 `SSH 连接失败`，再检查目标地址、端口、密钥和反向隧道；单纯的目录 `Permission denied` 表示 SSH 已经连通，只是远程用户没有相应文件权限。

## 10. 测试

后端：

```bash
python3 -m py_compile monitor.py
python3 -m unittest discover -s tests -v
```

浏览器回归先启动干净演示服务：

```bash
python3 monitor.py \
  --demo \
  --allow-kill \
  --allow-delete 3 \
  --bind=127.0.0.1 \
  --port=8765
```

另一个终端执行：

```bash
node tests/browser_test.mjs http://127.0.0.1:8765
```

浏览器测试需要本机安装 Chromium 或 Chrome。

独立页面冒烟检查：

```bash
chromium --headless=new --no-sandbox \
  --virtual-time-budget=2500 \
  --dump-dom "file://$(pwd)/doc/index.html"
```

## 11. 当前实现文件

```text
monitor.py                  后端、采集、缓存和 API
hosts.json                  可提交的脱敏默认配置
hosts.local.json            当前机器私有配置（Git 忽略）
doc/index.html              当前 Web 页面
doc/PRD.md                  产品说明书
doc/TECHNICAL_DESIGN.md     技术方案
tests/test_monitor.py       Python 后端测试
tests/browser_test.mjs      Chromium 端到端测试
```
