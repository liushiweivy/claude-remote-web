# Claude 会话台 · Session Bridge

在网页（电脑/手机同网段）里查看本机正在运行的 Claude Code 会话，并远程打字。
Windows 专用：FastAPI + pywinpty(ConPTY) + xterm.js，无构建步骤，原生 JS。

## 快速开始

```bat
:: 双击 start.bat（首次自动建 venv、装依赖）
start.bat
```

启动后控制台打印带 token 的访问地址并自动打开浏览器：

- 本机：`http://127.0.0.1:8765/?token=...`
- 手机/同网段：`http://192.168.1.100:8765/?token=...`（地址以启动窗口实际打印为准）

token 保存在 `data/token.txt`（首次启动自动生成）。网页令牌门通过后存 localStorage，
之后访问 `http://127.0.0.1:8765/` 即可。

## 两种接入方式

| | 托管新会话（主路径） | 接入已有会话 |
|---|---|---|
| 启动 | 网页点「＋ 托管新会话」选目录，后端 `cmd /c claude` | 本机任意终端自己跑的 claude.exe |
| 通道 | pywinpty ConPTY ↔ WebSocket ↔ xterm.js 全双工 | AttachConsole + WriteConsoleInput 注入 |
| 中文 | ✅ 正常 | ❌ 会叠字（见已知限制），英文/数字正常 |
| 关网页 | 进程继续跑，随时从列表重连 | 不影响原终端 |
| 附带 | 完整终端，与真终端等价 | 会话记录镜像（读 `~/.claude/projects` 的 jsonl） |

日常手机远程用「托管新会话」；「接入已有会话」适合临时给桌面上跑着的 claude 补一句英文命令。

## 手机访问

1. 手机连同一个 WiFi（和电脑同网段）。
2. 浏览器开启动窗口里的「局域网」地址（含 token）。
3. 打不开时查 Windows 防火墙：需要入站允许本项目 venv 的 python（端口 8765）。
   本机当前以太网为「公用」配置文件，曾在公网环境请先关端口或改用私有网络。

> 服务监听 `0.0.0.0:8765`，同网段任何设备持 token 即可得到本机终端等价能力，
> 不要在不可信网络里长期开放。端口可用环境变量 `CRW_PORT` 覆盖。

## 公网访问（cpolar）

本机 cpolar 配置（`C:\Users\4v\.cpolar\cpolar.yml`）里有 `web8765` 隧道指向 8765。
免费版域名是**随机的，cpolar 重启后会变**，当前地址以 cpolar 客户端/Web 面板显示为准：

```
https://<随机>.r39.cpolar.top/?token=<data/token.txt 的内容>
```

实测（2026-10-15）：页面、token 门、错误 token 401、托管会话 WSS 全链路经公网通过。
注意别复制错隧道：配置里另有一个 `website` 隧道指向 8080，本机 8080 无服务，
访问它的域名只会得到 502。

> **安全警告**：持完整链接（含 token）的任何人等同于拿到你电脑的交互式终端。
> 随机域名不是访问控制，token 是唯一防线。请勿外传链接；长期公网使用应加 cpolar
> HttpAuth 或前置带 TLS 的反代，用完即关隧道。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q     # 22 个用例
.\.venv\Scripts\python.exe -m server.app         # 直接启动（不走 start.bat）
```

静态文件在 `static/`（xterm.js 为本地 vendor，无 CDN 依赖）。改了 `app.js`/`styles.css`
记得升 `index.html` 里的 `?v=` 版本号，浏览器对静态资源缓存较激进。

自测脚本（非自动化套件，需服务在跑）：

```powershell
# 托管会话中文全链路探针：建一个托管会话拿到 id 后
.\.venv\Scripts\python.exe tools\probe_mgr_cjk.py <会话id>
# 输出 got_reply/cjk_echo_ok/cjk_triple_runs，原始帧写 tools/probe_out_<id>.txt
```

## 目录

```
server/
  app.py          FastAPI 路由：token 门、会话枚举、托管 CRUD、/ws/managed
  ptyman.py       托管会话：pywinpty 封装、env 清洗、进程树结束
  sessions.py     本机 claude.exe 枚举（PEB 读 cwd/cmdline，纯 ctypes）
  injector.py     控制台注入（AttachConsole / WriteConsoleInput）
  transcripts.py  ~/.claude/projects jsonl 增量解析
  winproc.py      Win32 API ctypes 层
static/           index.html / styles.css / app.js / vendor(xterm)
tests/            pytest（不依赖真 PTY）
tools/            开发期探针与调研脚本
data/             token.txt、托管会话工作目录
```

## 已知限制

- **外部会话中文叠字**：WriteConsoleInput 注入 CJK 到 Claude TUI 时 libuv 单份、
  TUI 双消费导致叠字；VK_PACKET / bracketed-paste 等均试过无效。ConPTY 写入通道
  （托管会话）没有此问题，中文请走托管会话。
- 重连已有托管会话时终端不清屏历史：ConPTY 不回放屏幕，xterm 靠 open 后的
  resize 触发 TUI 全量重绘。
- 结束托管会话用 `taskkill /PID <cmd根pid> /T /F`（pywinpty 无 close）。
- 多个浏览器 tab 同时接同一个托管会话会竞争 PTY 输出帧，正常只开一个。

## 安全说明

- token 出现在 URL（会进浏览器历史/启动窗口）与 WS query 中，仅适用于受信家庭网络；
  公网暴露请自行加反代 + TLS。
- 托管会话继承当前用户权限，等同于在本机开终端。
