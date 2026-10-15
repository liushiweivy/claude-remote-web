# Claude 会话台 — 进度断点（2026-10-15 13:45 更新：清单全部完成，详见 README.md）

## 这是什么
网页查看本机所有正在运行的 Claude Code 会话，并远程输入文字（手机/同网段可用）。
- 路径：`E:\1workspace\claude-remote-web`，端口 **8765**
- 启动：双击 `start.bat`（venv 已建好，依赖已装）
- 访问：`http://127.0.0.1:8765/?token=...`（token 在启动窗口 / `data/token.txt`，当前 `UGG19I9fQhu3zA6xZoj6zgux`）
- 服务器后台正在运行（task id `bavprk1ow`），可直接打开浏览器试

## 两条接入路径
1. **托管会话（主路径，中文正常 ✅）**：网页点「＋托管新会话」→ 后端 pywinpty(ConPTY) 起 `cmd /c claude` → 浏览器 xterm.js 全双工，和真终端完全等价；断开网页进程继续跑。
2. **已有会话远程输入**：枚举本机 claude.exe，AttachConsole+WriteConsoleInput 打字；transcript 镜像读 `~/.claude/projects` jsonl 看对话。**英文/数字可靠；中文叠字（平台限制，UI 已提示）**。

## 已完成并实测
- 进程枚举 + PEB 读 cwd/cmdline（纯 ctypes，7 个真实会话识别正确）
- 控制台注入：conhost 与 Windows Terminal(ConPTY) 均实测送达，raw/cooked 模式、Ctrl+C/Esc 均通
- ConPTY 托管通道起真 claude，中文任务「只回复 ZQ_MGR_OK…」一字不叠、模型正常回复（探针 probe_pty_claude.py 证实）
- transcript 解析（user/assistant/tool，增量 offset、残行不消费、截断重置）
- token 门、局域网地址显示、托管会话目录选择
- **22 个 pytest 全绿**；Playwright 真机打开列表/会话详情正常（唯一 console error 是 favicon 404）

## 收尾完成（2026-10-15）
1. ✅ 浏览器全流程：modal→启动→xterm 渲染 claude TUI；中文全链路由 tools/probe_mgr_cjk.py 实测（sid=4 模型正常回复，无叠字）。
   - 顺带抓到并修复终端黑屏根因：index.html 第 105 行误写 `</div>` 关闭 `<header>`，HTML5 解析器把 #terminal 重排到 body 下脱离 grid 轨道；已改 `</header>`。
   - 配套：.xterm 改 absolute/inset 获得确定尺寸；resize 回发加 lastDims 去重；静态资源升 ?v=20261015b。
2. ✅ start.bat 核对为 UTF-8 无 BOM + chcp 65001，中文正常；venv 已存在（首次建环境分支未实测）。
3. ✅ favicon 改内联 SVG（铜金"鉴"字），无 404。
4. ✅ 局域网：0.0.0.0:8765，本机经 192.168.1.100 hairpin 自测通过；**手机真机访问待用户点 URL 验证**（防火墙已存在 python.exe Public 入站规则）。
5. ✅ README.md 已写。
6. 可选研究（未做）：外部会话中文叠字试 SendInput KEYEVENTF_UNICODE。

## code review 后追加修复（2026-10-15）
- HIGH：openManaged 的 window resize 监听 + ResizeObserver 改由 state.managedCleanup 在 closeManagedWs 中摘除（原实现每开一次会话累积一组闭包）。
- HIGH：「结束会话」改为先 closeManagedWs 再发 DELETE，杜绝零尺寸 resize 发往将死 PTY；浏览器按钮实测杀树成功（sid=4）。
- MEDIUM：API 请求 token 从 URL query 改 `X-Auth-Token` 头（WS 浏览器无法自定义头，仍走 query）；注意 `/api/health` 只认 query，首屏健康检查保留 `?token=`。
- MEDIUM：外部会话退出后标题「（已退出）」每 5 秒重复追加 → data-dead 守卫。
- 小修：ext-meta 对 textContent 误 esc 双重转义；探针强制显式 sid 参数。
- 资源版本号：?v=20261015d；22 pytest 全绿；console 零警告；备用端口冒烟通过。

## 遗留观察项
- 重连已存在托管会话时 ConPTY 不回放屏幕，靠 ws.onopen 的 resize 逼 TUI 重绘；别同时开两个 tab 接同一会话（竞争 PTY 帧）。
- 服务进程 PID 17312 仍在 8765 运行（上次会话 detached 启动）；重启走 start.bat。
- 2026-10-15 追加：start.bat 已从 UTF-8 中文版改为**纯 ASCII**（实测含中文的 UTF-8 bat 被 cmd 按 GBK 错位解析整文件报废，chcp 65001 无效；详见 topics 档案第 19 条），CRW_PORT=8767 冒烟通过。
- 独立窗口启动器：`tools/start_cc_window.py`（CREATE_NEW_CONSOLE + 清洗 env + argv 传中文首条，无叠字）；新窗口进 Windows Terminal 新 tab 而非独立 conhost 窗。

## 关键坑（详见 E:\1aimemory\topics\claude-remote-web.md）
- WriteConsoleInput 注入 CJK 到 Claude TUI 必叠字（libuv 层单份，TUI 双消费；VK_PACKET/VK_PROCESSKEY/bracketed-paste 均无效）；ASCII 不叠。**ConPTY 写入通道无此问题**。
- 子进程继承 `CLAUDE_CODE_CHILD_SESSION=1` 会让新 claude 关闭 transcript 落盘（屏显警告）。启动 claude 必须清洗 7 个 CLAUDE_* 变量并设 `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1`（ptyman.clean_env 已做）。
- pywinpty：`PTY.read(blocking=False)` 只收 0-1 个参数；无 `close()`，结束用 `taskkill /PID <cmd根pid> /T /F`。
- ctypes x64：HANDLE restype 不声明会被截断成 32 位；PBI 48 字节；RTL_USER_PROCESS_PARAMETERS 偏移实测 0x38/0x60/0x70。
- AttachConsole 后 GetStdHandle 拿到的是旧句柄（err=6），必须 CreateFileW("CONIN$")；AttachConsole 是进程级状态需加锁。
- bash 工具 cwd 每次重置到 C:\Users\4v，命令一律用绝对路径（曾两次误建家目录 .venv）。
