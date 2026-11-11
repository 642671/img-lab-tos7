# TOS 7 规范对照与实机验收

本文记录 `img-lab-tos7` 对 `TOS7_APP_IFRAME_DEVELOPMENT_NOTES.md` 和 `tos-app-pkg-tools/` 中关键约束的落实情况，以及仍需在真实 TOS 设备上完成的验收项。

## 1. 包元数据

| 项目 | 实现 |
| --- | --- |
| 应用 ID | `img-lab` |
| 包名 | `img-lab` |
| systemd ID | `imglab-system` |
| iframe 路径 | `/img-lab/` |
| 图标 | `/images/icons/img-lab.svg` |
| 发布者/作者 | `Yuii` |
| 分类 | `Utilities` |
| 最低版本 | `TOS7.0` |
| 依赖 | `python3 (>= 3.10), systemd` |
| 语言 | 14 个 TOS 必需语言段落，均含 `name/auth/descript/release_note/important` |
| 可选链接 | `help` 和 `official` 留空（规范允许为空） |

作者字段只影响本应用。现有其他应用的作者信息未修改。

## 2. systemd 与用户

| 约束 | 实现 |
| --- | --- |
| 不设置 `Group=<appid>` | unit 中只有 `User=img-lab`，主组由 `useradd --gid allusers` 决定 |
| 不启用 `PrivateTmp` | unit 中没有该指令 |
| 不修改 `/var/api` 所有者/权限 | `postinst` 仅在目录不存在时创建并设为 `1777`；已存在时不动 |
| 有界停止 | `TimeoutStopSec=10`、`KillSignal=SIGTERM`、`SendSIGKILL=yes` |
| 自动重启 | `Restart=on-failure`、`RestartSec=10`、启动限速 5 次/60 秒 |
| socket 清理 | `ExecStopPost=-/bin/rm -f /var/api/img-lab.sock`，服务内部也按 inode 身份清理 |
| 资源限制 | `MemoryMax=1G`、`CPUQuota=200%`、`LimitNOFILE=65536`、`LimitNPROC=256` |
| 可写路径 | 仅 `data/`、`logs/` 和共享 `/var/api`；`ProtectSystem=strict` 下其余只读 |

### 安装流程

`DEBIAN/postinst` 会：

1. 确保 `data/` 和 `logs/` 存在。
2. 创建系统用户 `img-lab`：优先使用 `allusers` 组，否则创建同名主组。
3. 设置数据/日志目录所有者和权限。
4. 解压 `webui.bz2` 到 `webui/`。
5. 仅在 `/var/api` 缺失时创建并设置 `1777`。
6. 复制 unit、`daemon-reload`、`enable`、`reset-failed`。
7. 若存在陈旧 socket（`-S` 判定），由 root 删除。
8. 启动服务并最多等待 25 秒，验证 `is-active` 且 socket 存在。
9. 失败时写入 `/usr/local/img-lab/logs/postinst-diagnostic.txt` 并仍以 0 退出，避免 dpkg 卡死。

`prerm` 停止并禁用服务，只删除 `-S` 判定为 socket 的路径，不删除同名普通文件。`postrm` 在 `remove|purge` 时移除 unit 和应用目录，在 `purge` 时移除用户。

## 3. 后端与 socket

- Python 3.10+ 标准库；无第三方运行时依赖。
- `AF_UNIX` 不可用时（如 Windows 开发环境）回退 TCP，便于测试；生产 unit 始终走 Unix socket。
- `prepare_socket` 会拒绝替换普通文件、清理陈旧 socket、按需创建目录，并在启动后设置 socket 权限。
- 关闭时仅当 socket 的 `(st_dev, st_ino)` 与启动时一致才删除，避免误删其他实例的 socket。
- `SIGTERM`/`SIGINT` 由独立线程触发 `shutdown`，避免在信号处理函数中直接等待服务器线程导致死锁。
- 生成任务在后台线程执行，最多保留 20 个任务记录；同一时间只允许一个生成任务和一个测速任务。

## 4. HTTP 与 iframe 代理

- 路由归一化支持：
  - `/v2/proxy/img-lab/...`
  - `/img-lab/...`
  - 裸路径 `/health`、`/api/...`
- 健康检查返回 JSON，正文包含 `"app": "img-lab"`、`"ok": true`、版本、socket、编码器能力和图片库统计。
- 前端不会仅凭 HTTP 200 判定可用；必须同时满足 JSON Content-Type、`ok` 和 `app` 匹配。
- CSP：`default-src 'self'`、`script-src 'self'`、`frame-ancestors *`，禁止内联脚本；主题脚本独立为 `theme.js`。
- 安全响应头：`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。
- 静态文件白名单 + `realpath` 包含检查，阻止目录穿越。
- HTML 使用 `no-store`；CSS/JS/SVG 使用缓存并带 `?v=<version>`。
- `OPTIONS` 仅对显式配置的来源返回 CORS 头；默认不开放跨域。
- POST 要求 `Content-Type: application/json`，请求体上限 64 KiB，单次批量最多 50 条。
- 前端 GET 不设置 `Content-Type`，所有请求带 `credentials: "include"`；POST 在存在 `X-Csrf-Token` Cookie 时附带同名请求头。

## 5. 前端缓存与主题

- `index.html` 中所有静态资源均带 `?v=__APP_VERSION__`，构建时替换为真实版本。
- 页面外层 `data-theme="light"`，`theme.js` 读取：
  1. 自身 `localStorage["imgLab.theme.v1"]`；
  2. 父窗口 `localStorage["themeMode"]`；
  3. `prefers-color-scheme`。
- 无内联脚本；所有动态文本通过 `textContent` 或 `escapeHtml` 写入，避免 XSS。
- 窄屏断点下表单与结果区改为单列，页面不产生横向滚动。

## 6. 生成与体积真实性

- 目标体积校验：至少 1 KiB、最多 256 MiB；单张输出上限 256 MiB。
- 像素上限：JPEG/WebP 60 MP，PNG 25 MP；分辨率 128–16384 px，且为 8 的倍数。
- 第一级按先验 bpp 求分辨率，最多 6 轮，容差 5%；修正系数夹在 0.55–1.75。
- 第二级固定分辨率调整噪声混合权重，最多 9 轮，阻尼 0.85，最终容差 1.5%。
- 质量参数固定；分辨率或目标变化时才改变尺寸。
- 未收敛时保留最接近结果，在 `converged=false`、`note` 和界面中明确标记。
- 每次输出都从内存编码结果读取真实字节数；图片库加载时以 `stat().st_size` 覆盖索引记录。
- CSV 使用 UTF-8 BOM 和 CRLF，`actual_bytes` 与磁盘文件一致。
- 固定 seed 与固定内容模型，保证相同规格的渲染结果确定。

## 7. 编码器与资源

- PNG：纯标准库 `zlib` 编码，始终可用。
- JPEG：`ctypes` 绑定系统 `libjpeg`，启动时 `fork` 子进程做 ABI 探测，避免错误 ABI 直接崩溃主进程。
- WebP：`ctypes` 调用 `WebPEncodeRGB`，仅在库可用时启用。
- 缺少动态库时通过 `/api/options` 和界面明确展示不可用。
- 渲染采用条带分块，避免一次性申请整幅 4 通道缓冲；48 MP 输出受 1 GiB `MemoryMax` 约束。
- 开发机 WSL 实测 48 MP：渲染约 1.1 秒，JPEG 编码约 0.2 秒，WebP 编码约 23 秒；5/8 MiB WebP 目标分别约 6/32 秒。前端在超过 25 MP 时提示 WebP 编码明显变慢。
- 图片库最多 200 条、约 1.5 GiB，按时间清理最旧条目。

## 8. 测速边界

- 仅接受 `http` / `https`，拒绝凭据、非法端口和超长 URL。
- 并发 1–16、轮数 1–20、总请求不超过 160，单请求超时 1–60 秒。
- 跟随最多 3 次重定向；正文读取上限 32 MiB，超出标记 `truncated`。
- 失败率 = 失败请求数 / 总请求数；HTTP 4xx/5xx 计为失败。
- 分阶段计时：连接、发送、等待首字节、下载正文。
- 服务端时间优先读取 `Server-Timing` 中带 `dur=` 的项，其次 `X-Response-Time`，再次 `X-Runtime`；无法解析时不展示。

## 9. 已执行的本地验证

在 Windows 开发环境执行：

```powershell
python -m unittest discover -s backend/tests -v
python -m compileall -q backend
node --check frontend/theme.js
node --check frontend/tools.js
node --check frontend/app.js
node --test tests/frontend_tools.test.js
python build.py --platform x86_64
python build.py --platform aarch64
```

Windows 会跳过 POSIX socket 生命周期测试；缺少 `libjpeg`/`libwebp` 时跳过对应编码测试。需要在 WSL/Linux 或 TOS 实机上补充 JPEG/WebP 标定测试。

## 10. 仍需实机验收

- `dpkg -i` 安装、`postinst` 诊断、服务 active、socket 出现。
- TOS iframe 代理路径、登录会话携带、CSRF Cookie。
- `libjpeg` / `libwebp` 实际 ABI 与编码结果。
- 1/5/8 MiB 标定在目标硬件上的偏差与耗时。
- 48 MP JPEG 是否在 10 秒内完成；WebP 大图编码耗时、内存占用与任务取消响应。
- 图片库容量、磁盘配额和清理行为。
- 测速在真实网络与 TOS 网关下的计时准确性。
