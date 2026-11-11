# CLAUDE.md

本文件用于约束在 `img-lab-tos7/` 中进行的自动化开发工作。根目录的 `AGENTS.md` 和 `TOS7_APP_IFRAME_DEVELOPMENT_NOTES.md` 仍然适用；如果内容冲突，以更具体的提醒和用户当次要求为准。

## 项目定位

这是 TOS 7 的离线测试图片生成与响应速度测量应用，不是通用图片编辑器或图床客户端。

- 应用 ID：`img-lab`
- 显示名称：图片实验室
- 作者/发布者：`Yuii`
- 当前版本：以 `VERSION` 为准
- 后端：Python 3.10+ 标准库，禁止新增第三方运行时依赖
- 前端：原生 HTML/CSS/JavaScript，不引入构建链
- 平台：TOS 7 x86_64 / aarch64
- 数据目录：`/usr/local/img-lab/data`
- socket：`/var/api/img-lab.sock`

## 不可破坏的约束

1. **体积必须真实**：不得用固定分辨率伪造目标体积；不得在超出容差时静默通过。未收敛必须在 `note`、CSV 和界面中明确标记。
2. **离线**：生成流程不得依赖网络、CDN 或远程字体；前端所有资源本地化。
3. **编码器诚实**：缺少 `libjpeg`/`libwebp` 时必须在能力和界面中标注不可用，不能回退成假数据。
4. **字节数一致**：图片库、CSV、ZIP 中显示的字节数必须与磁盘文件一致；索引加载时以 `stat().st_size` 为准。
5. **不修改现有应用**：只在 `img-lab-tos7/` 内工作，不改动其他项目及其作者信息。
6. **不执行破坏性操作**：未经用户明确同意，不重启 NAS、不卸载生产应用、不运行 `git clean`/`git reset --hard`、不删除构建产物。
7. **不创建提交/标签/推送/Release**，除非用户明确要求。
8. **TOS 约束**：
   - 不设置 `Group=<appid>`。
   - 不启用 `PrivateTmp=true`。
   - 不修改 `/var/api` 的所有者或权限（仅在缺失时创建 `1777`）。
   - `postinst` 必须 `reset-failed`、启动并验证 active + socket，失败时写诊断。
   - 服务必须处理 `SIGTERM`，`TimeoutStopSec=10`。
   - 前端静态资源必须带版本查询参数。
   - 健康检查必须校验响应正文中的 `app: img-lab`。
   - 浏览器请求使用 `credentials: include`；GET 不设置 `Content-Type`。
   - 生命周期脚本只用 POSIX `sh` 语法。

## 修改流程

1. 先读 `README.md`、`CHANGELOG.md`、`VERSION`、`build.py`、`DEBIAN/control`、systemd unit 和相关测试。
2. 检查子仓库与根仓库的 Git 状态，保留用户已有改动：

   ```powershell
   git -C D:\111\dev-test\img-lab-tos7 status --short --branch
   git -C D:\111\dev-test status --short --branch
   ```

3. 只改与任务直接相关的文件，不做无关重构。
4. 版本变化时同步：`VERSION`、`config.ini`、`DEBIAN/control`、`backend/img_lab/__init__.py`、14 个语言段落的 `release_note`、`CHANGELOG.md`。
5. 构建由 `build.py` 生成，不能手动修改 `dist/` 中的包来伪造结果。

## 测试与构建

```powershell
cd D:\111\dev-test\img-lab-tos7
python -m unittest discover -s backend/tests -v
python -m compileall -q backend

node --check frontend/theme.js
node --check frontend/tools.js
node --check frontend/app.js
node --test tests/frontend_tools.test.js

python build.py --platform x86_64
python build.py --platform aarch64
```

Windows 上会跳过 POSIX socket 测试；缺少 `libjpeg`/`libwebp` 时跳过对应编码测试。最终交付必须明确列出未执行的验证，不能声称已通过。真实 TOS 安装、iframe 代理和编码性能仍需实机验收。

## 当前实现要点

- 内容模型：96×72 单元网格覆盖水平渐变，单元格颜色抖动 ±24；高频噪声按 64 个条带分块逐像素异或。
- 噪声幅度：photo `base=24, high=32, low=16`，gradient `0–4`，solid `0–6`，noise 全 0。
- 校准先验：JPEG q85 照片纹理 0.239 bpp；WebP q85 0.257 bpp；内容因子与 PNG bpp 同时存在于后端 `sizing.py` 和前端 `tools.js`。
- 第一级最多 6 轮、目标 5%，修正系数夹在 0.55–1.75；第二级最多 9 轮、阻尼 0.85、目标 1.5%。
- 单张输出上限 256 MiB；像素上限 JPEG/WebP 60 MP、PNG 25 MP；48 MP 的渲染与 JPEG 生成需在 10 秒内完成。WebP 大图编码明显更慢（实测 48 MP 约 23 秒），前端在超过 25 MP 时必须提示，不能承诺 10 秒完成。
- 渲染必须确定性：固定 seed、相同规格产生相同字节。
- SVG 图标为 64×64 viewBox，构建会拒绝脚本和事件属性。
- 作者字段固定为 `Yuii`；`DEBIAN/control` 邮箱是占位值，正式发布前需确认。Homepage 留空，不要填写不存在的仓库地址。

## 目录指南

- `backend/img_lab/content.py`：确定性像素渲染与探针。
- `backend/img_lab/encoders.py`：PNG 编码、libjpeg/libwebp ctypes 绑定与能力探测。
- `backend/img_lab/sizing.py`：体积目标校验、两级求解器、Outcome。
- `backend/img_lab/library.py`：图片库、CSV 清单、ZIP 打包、清理。
- `backend/img_lab/speedtest.py`：HTTP 测速、分阶段计时、服务端时间解析。
- `backend/img_lab/server.py`：HTTP 路由、静态文件、Unix socket 生命周期、任务管理。
- `frontend/tools.js`：格式化、先验估算、规格解析，UMD 供 Node 测试复用。
- `frontend/app.js`：API 探测、表单、任务轮询、图片库、测速渲染。
