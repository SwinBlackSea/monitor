# 极简多机进程监视器

一个面向 3–10 台 Linux 机器的轻量监视器：Python 标准库后端、原生 Web 前端、SSH 远程采集、目标机零 Agent。

极简原则：优先复用单进程、标准库、系统命令和内存缓存；没有明确收益时，不引入框架、数据库、消息队列、常驻 Agent 或额外部署层。

主要能力：

- 多机器 Tab 与 SSH 连通状态。
- CPU、内存进程排行，支持全列排序和终止进程。
- 挂载点与按需展开的目录占用树。
- 多浏览器共享每台机器的最新内存快照。
- 单文件后端、单文件前端、无数据库和前端构建链。

## 文档

长期维护三份说明：

- 本 README：启动、访问和常用操作。
- [产品说明书](doc/PRD.md)：页面行为、交互和验收标准。
- [技术方案](doc/TECHNICAL_DESIGN.md)：架构、采集、缓存、接口、安全和测试。

Web 原型与正式页面均为 [doc/index.html](doc/index.html)。

## 快速体验

环境要求：Linux、Python 3.10+；监控远程机器时还需要系统 `ssh`。

```bash
python3 monitor.py --demo --allow-kill --allow-delete 3
```

访问：

```text
http://127.0.0.1:8080
```

演示模式使用模拟机器，不访问真实服务器。也可以直接用浏览器打开 `doc/index.html` 查看离线模拟页面。

## 正式启动

只监控本机：

```bash
python3 monitor.py
```

允许终止进程，并允许从第 3 级目录开始清空内部内容：

```bash
python3 monitor.py --allow-kill --allow-delete 3
```

默认只监听 `127.0.0.1:8080`。

### 局域网或公网 IP

监听非本机地址时必须设置 Web 登录：

```bash
MONITOR_USERNAME=admin \
MONITOR_PASSWORD='替换为强密码' \
python3 monitor.py \
  --allow-kill \
  --allow-delete 3 \
  --bind=0.0.0.0 \
  --port=8080
```

访问 `http://服务器IP:8080/`，在浏览器 Basic Authentication 弹窗中输入账号密码。

程序本身只提供 HTTP。明文公网访问不能保护登录信息和管理操作，只适合临时测试；长期使用域名或公网访问时，应让 Caddy、Nginx 或可信 VPN 提供 HTTPS，并把请求转发到仅监听回环地址的 `monitor.py`。

若认证弹窗被浏览器遮挡，可临时使用：

```text
http://{account}:{passwd}@{IP}:8080/
```

密码中的 `@`、`:`、`/` 等字符必须 URL 编码。该地址可能进入浏览器历史，只建议临时排查。

### 域名与 HTTPS

推荐后端启动方式：

```bash
MONITOR_USERNAME=admin \
MONITOR_PASSWORD='替换为强密码' \
python3 monitor.py --bind=127.0.0.1 --port=9500
```

再由反向代理将 HTTPS 域名转发到 `http://127.0.0.1:9500`。

## 添加远程机器

先确保运行 `monitor.py` 的系统账户能够免交互 SSH 登录：

```bash
ssh -o BatchMode=yes -p 22 user@server true
```

然后在页面顶部点击 `＋`，填写机器名称、IP 或主机名、SSH 用户和端口。“测试连接”只验证，“保存”才会写入配置。网页不接收 SSH 密码或私钥。

仓库中的 `hosts.json` 是脱敏示例。真实部署建议：

```bash
cp hosts.json hosts.local.json
```

编辑 `hosts.local.json` 后启动服务。该文件已被 Git 忽略，并会优先于 `hosts.json` 加载；也可以使用 `--hosts` 指定其他文件。

## 常用参数

| 参数 | 作用 |
|---|---|
| `--demo` | 使用模拟数据 |
| `--allow-kill` | 开启 CPU/内存页面的进程终止功能 |
| `--allow-delete N` | 从第 N 级开始允许清空目录内容，N 不小于 3，目标目录保留 |
| `--bind` | HTTP 监听地址，默认 `127.0.0.1` |
| `--port` | HTTP 端口，默认 `8080` |
| `--interval` | 当前机器快照周期，默认 5 秒 |
| `--hosts` | 指定机器配置文件 |

Web 登录也可使用 `--username`、`--password`，推荐通过 `MONITOR_USERNAME`、`MONITOR_PASSWORD` 环境变量提供。

## 重要说明

- 终止进程和清空目录默认关闭，并使用运行账户或远程 SSH 用户的系统权限。
- `--allow-delete N` 清空目标目录内部内容，不删除目标目录本身。
- CPU、内存和进程共用当前机器的一份快照；硬盘目录只在点击时查询。
- 机器配置持久化到 JSON；快照、连通状态和目录结果只保存在内存，不记录历史数据。
- 反向 SSH 隧道、公网地址识别、缓存策略和权限错误处理见[技术方案](doc/TECHNICAL_DESIGN.md)。

## 测试

后端回归：

```bash
python3 -m py_compile monitor.py
python3 -m unittest discover -s tests -v
```

浏览器回归需要 Chromium。先启动演示服务：

```bash
python3 monitor.py --demo --allow-kill --allow-delete 3 --port=8765
```

再在另一个终端执行：

```bash
node tests/browser_test.mjs http://127.0.0.1:8765
```

完整覆盖范围和实现限制见[技术方案](doc/TECHNICAL_DESIGN.md)。
