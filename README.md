# 图片实验室（Image Lab）

适用于 TOS 7 的离线测试图片生成与响应速度测量应用。按分辨率或按目标字节数生成 JPEG / PNG / WebP 测试图片，用真实编码结果反复校准尺寸，导出 CSV 清单与 ZIP 打包，并测量目标 HTTP 地址的响应耗时。

- 应用 ID：`img-lab`
- 当前版本：`0.1.0`
- 作者：Yuii
- 分类：Utilities
- 平台：TOS 7（x86_64 / aarch64）
- 许可证：MIT

## 功能

### 测试图片生成

- 按分辨率生成：指定宽高（8 的倍数，128–16384 px）、格式、JPEG/WebP 质量。
- 按目标体积生成：给定目标字节数，自动求解分辨率并校准到 |偏差| ≤ 1.5%。
- 生成前计划预览：预计分辨率、像素量、估算体积。
- 生成后实测数据：真实像素、实际字节数、偏差百分比、是否收敛、迭代次数和耗时。
- 批量生成：每行一个规格（`1920x1080` 或 `5 MiB`），单次最多 50 条，完成后统一打包下载。
- CSV 清单（UTF-8 BOM、CRLF），字节数与磁盘文件完全一致。
- 内容模型：照片纹理（默认）、平滑渐变、纯色块、高频噪声。
- 下采样探针：在边角绘制黑白条纹块，便于检查缩略图/下采样链路。

### 响应速度测量

- 指定目标 URL、并发数（1–16）、轮数（1–20）、单请求超时。
- 输出 p50 / p95 / 均值 / 最小值 / 最大值与失败率。
- 将网络阶段拆分为连接、发送、等待首字节、下载正文。
- 识别 `Server-Timing: dur=`、`X-Response-Time`、`X-Runtime` 响应头，单独展示服务端处理时间。
- 支持取消、跟随最多 3 次重定向、32 MiB 正文上限。

### 体积校准算法

1. **第一级**：按先验 bpp（每像素字节数）估算像素数 → 生成 → 实测 → 用实测 bpp 修正，最多 6 轮，目标 5% 以内。
2. **第二级**：固定分辨率，调整连续高频噪声的混合权重，用 0.85 阻尼的割线法做线性外推，最多 9 轮，收敛到 1.5% 以内。
3. 质量参数始终固定，只让分辨率随目标变化。
4. 无法收敛时保留最接近的结果，并在 `note` 和界面中明确标记，绝不静默输出超差结果。

标定基准（JPEG q85、4:3、照片纹理）：

| 目标 | 参考分辨率 | 参考字节 |
| --- | --- | --- |
| 1 MiB | 2412×1810 | 1,043,372 |
| 5 MiB | 5726×4294 | 5,283,396 |
| 8 MiB | 7362×5522 | 8,445,481 |

## 安装

### 安装包

```text
dist/img-lab_0.1.0_x86_64.deb
dist/img-lab_0.1.0_aarch64.deb
dist/img-lab_0.1.0_x86_64.deb.sha256
dist/img-lab_0.1.0_aarch64.deb.sha256
```

### 手动安装

```sh
sudo dpkg -i img-lab_0.1.0_x86_64.deb
```

`postinst` 会创建应用用户、解压前端、安装并启动 systemd 服务，并最多等待 25 秒确认服务 active 且 socket 出现。启动失败时诊断信息写入：

```text
/usr/local/img-lab/logs/postinst-diagnostic.txt
```

## 使用

1. 打开“图片实验室”，等待右上角连接状态变为“已连接”。
2. 在“生成”页选择“按分辨率”或“按目标体积”。
3. 设置格式、质量、内容和是否绘制探针。
4. 查看计划卡片，确认后点击“开始生成”。
5. 生成完成后在“图片库”中预览、下载单张、导出 CSV 或打包 ZIP。
6. 在“测速”页输入 URL 和并发/轮数，查看分阶段耗时与失败率。

## 数据与配置

| 路径 | 用途 |
| --- | --- |
| `/usr/local/img-lab/data/library/index.json` | 图片库索引 |
| `/usr/local/img-lab/data/library/<id>.<ext>` | 生成的图片文件 |
| `/usr/local/img-lab/logs/` | 安装诊断与会话日志 |
| `/var/api/img-lab.sock` | TOS iframe 代理使用的 Unix socket |

图片库最多保留 200 条、总计约 1.5 GiB，超出时按时间从旧到新清理。所有显示的字节数在读取时以磁盘文件大小为准。

环境变量位于 `/usr/local/img-lab/img-lab.env`：

```ini
IMG_LAB_DATA_DIR=/usr/local/img-lab/data
IMG_LAB_FRONTEND_DIR=/usr/local/img-lab/webui
IMG_LAB_SOCKET=/var/api/img-lab.sock
IMG_LAB_LOG_LEVEL=INFO
```

## 后端接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查，返回 `app: img-lab`、版本、编码器能力、图片库统计 |
| GET | `/api/options` | 格式、内容、默认值、限制、先验参数 |
| GET | `/api/library` | 图片库条目与容量限制 |
| GET | `/api/jobs` | 当前任务列表 |
| GET | `/api/jobs/<id>` | 单个任务状态与结果 |
| GET | `/api/files/<id>` | 下载图片库中的文件 |
| GET | `/api/manifest.csv[?ids=...]` | 导出 CSV 清单 |
| GET | `/api/bundle.zip[?ids=...]` | 打包图片与清单 |
| GET | `/api/preview?...` | 生成小尺寸预览 |
| POST | `/api/generate` | 开始生成（单条或批量） |
| POST | `/api/speedtest` | 开始测速 |
| POST | `/api/jobs/<id>/cancel` | 取消任务 |
| POST | `/api/library/delete` | 删除指定条目 |
| POST | `/api/library/clear` | 清空图片库 |

## 编码器说明

- PNG 始终可用，由 Python 标准库 `zlib` 直接编码，无损。
- JPEG 通过 `ctypes` 调用系统 `libjpeg`（内存目标），启动时用子进程探针验证 ABI。
- WebP 通过 `ctypes` 调用系统 `libwebp`（`WebPEncodeRGB`）。
- 缺少对应动态库时，能力和界面会明确标注不可用，不会伪装成功。
- 首版不引入 Pillow 或其他第三方 Python 依赖。

## 构建

需要 Python 3.10+ 标准库，不需要第三方 Python 依赖。

```powershell
cd img-lab-tos7
python build.py --platform x86_64
python build.py --platform aarch64
```

构建会校验版本、配置、语言文件、图标、systemd 契约、前端资源和后端模块，然后生成确定性的 `.deb` 与 `.sha256`。

## 测试

```powershell
python -m unittest discover -s backend/tests -v
python -m compileall -q backend

node --check frontend/theme.js
node --check frontend/tools.js
node --check frontend/app.js
node --test tests/frontend_tools.test.js
```

Windows 开发机会跳过 POSIX socket 测试；缺少 `libjpeg`/`libwebp` 时跳过对应编码测试，PNG 测试始终运行。真实 TOS 安装、iframe 代理和编码性能仍需实机验收。

## 已知限制

- 按体积校准依赖当前内容模型与编码器版本；不同 libjpeg/libwebp 构建的字节数会有差异，但应用会以实测结果为准继续校准。
- WebP 在 25 MP 以上编码明显变慢：开发机 WSL 实测 48 MP 约 23 秒、8 MiB 目标约 32 秒；JPEG 48 MP 约 1.5 秒。计划卡片会在超过 25 MP 时提前提示。
- 超小目标（约 1 KiB 以下）可能低于最小可编码体积，此时会明确标记未收敛。
- 测速的服务端时间仅在目标返回上述响应头时可用；否则只展示网络阶段。
- 测速只接受 `http` / `https`，不支持自定义请求头和认证凭据。

## 目录

完整结构见 [`docs/PROJECT_LAYOUT.md`](docs/PROJECT_LAYOUT.md)，TOS 规范对照见 [`docs/TOS7_SPEC_COMPLIANCE_CN.md`](docs/TOS7_SPEC_COMPLIANCE_CN.md)。
