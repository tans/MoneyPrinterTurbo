# pywebview + DaisyUI 桌面版

桌面入口是 `desktop.py`，视频、配音、字幕、素材下载继续使用原来的 Python 服务。DaisyUI 5 / Tailwind CSS 4 的样式随程序提供，运行时无需 Node.js 或 CDN。

## 启动

需要 Python 3.11+。Windows 需要 Microsoft Edge WebView2 Runtime；macOS 使用系统 WKWebView。

```bash
uv sync --frozen --extra desktop
uv run --no-sync python desktop.py
```

Linux 推荐 Qt（PySide6）后端：

```bash
sudo apt-get install libnss3 libasound2t64 libxkbcommon0 libegl1 libopengl0 libxtst6 libxkbfile1 libxcb-cursor0 fonts-noto-cjk
uv sync --frozen --extra desktop --extra desktop-linux
PYWEBVIEW_GUI=qt uv run --no-sync python desktop.py
```

需要图形桌面。发行版使用不同的系统库包名时，请参考 [pywebview 安装说明](https://pywebview.flowrl.com/guide/installation)。服务器调试可用 `python desktop.py --browser --port 8765`，在同一台机器的浏览器打开终端打印的地址。`--debug` 开启开发者工具，`--data-dir /path/to/data` 可指定数据目录。

## 使用与数据

- **创作工作台**：主题与文案、素材关键词、本地素材、模型音色、完整配音试听、字幕、配乐及全部 `VideoParams` 高级参数。支持生成视频，或只生成音频、字幕、素材。需要 API 的功能沿用原服务的账号配置。
- **素材库**：导入视频、图片、音频、背景音乐和字体；使用原有素材校验。文件会复制到用户数据目录，不直接依赖原文件的位置。
- **任务历史**：队列、进度、日志、取消、参数恢复、产物预览和另存为。SQLite 保留历史；退出或异常中断的任务下次启动标为“中断”，不会自动重发付费请求。
- **设置**：模型、TTS、素材服务配置、连接测试、高级 JSON 配置和缓存清理。设置保存到本地 `config.toml`；新任务采用提交时的配置快照。
- **预设**：导入 / 导出生成参数，兼容原工作台的设置预设。排除本机媒体路径和 API 配置，换机器后重新选择素材。
- **完整兼容工作台**：桌面内单独打开原 Streamlit 页面，覆盖尚未迁移的工作流。桌面任务结束后才能打开；打开期间暂停桌面配置写入和新任务提交。关闭兼容窗口后会重新加载配置和桌面页面，避免两个进程覆盖设置。关闭主窗口会关闭兼容工作台进程。

默认数据目录：Windows `%LOCALAPPDATA%/MoneyPrinterTurbo`；macOS `~/Library/Application Support/MoneyPrinterTurbo`；Linux `$XDG_DATA_HOME/MoneyPrinterTurbo` 或 `~/.local/share/MoneyPrinterTurbo`。其中保存配置、导入素材、`models/whisper-*`、`storage/tasks` 产物、缓存和 `desktop.sqlite3`。安装目录保持只读。升级不会覆盖已有配置和同名资源。

源码首次启动使用示例配置，不自动复制旧密钥。迁移旧配置可退出程序后，把原 `config.toml` 复制到上述数据目录，或在设置页重新填写。`MPT_DATA_DIR` 也适用于原 CLI/API/WebUI，未设置时保持原有仓库目录行为。

## 任务和费用

界面通过带会话 Cookie 的本机 `127.0.0.1` API 访问服务，端口自动分配。相同数据目录只允许一个主程序。任务串行运行，每条任务在独立 `spawn` 子进程中执行；取消和退出会终止该进程及其 FFmpeg 等子进程。

AI 素材、AI 配乐和自动发布均要求界面确认。Loomloom 先读取真实报价，报价绑定参数、选项和账号配置；使用固定请求 ID，已提交的报价不会重复使用。**取消本地任务无法撤销已经提交到远端的生成、扣费或发布**；可从任务日志 / 远端服务核对结果。API 密钥只进入配置文件和执行进程，不进入任务参数；常见密钥字段会从任务日志和错误中脱敏。数据目录仍应视作个人私有文件。

## 修改界面

```bash
npm ci
npm run build:desktop
npm run check:desktop
```

提交 `desktop/ui/app.css`，保证普通用户不必安装 Node.js。前端用原生 JavaScript 和 DaisyUI 组件；本机文件选择、另存为和打开目录通过 pywebview 的有限桥接方法完成。

## 构建桌面发行包

在目标操作系统构建，不能在 Linux 上生成 Windows / macOS 包：

```bash
uv sync --frozen --extra desktop --group desktop-build
# Linux 增加 --extra desktop-linux
uv run --no-sync python desktop/prepare_resources.py
uv run --no-sync pyinstaller --noconfirm desktop/desktop.spec
```

输出 `dist/MoneyPrinterTurbo/`，macOS 另有 `.app`。采用目录包，避免每次启动解压大型 Python / ML 依赖。FFmpeg 随 `imageio-ffmpeg` 收集；Whisper 模型不内置，由原引擎按需下载或手工放入数据目录。构建只包含固定版本并校验 SHA-256 的 Noto CJK 字体及其 OFL 许可，不把原仓库字体与背景音乐整体重新分发。用户可以自行导入有使用权的资源。

可运行 `uv run --no-sync python desktop/smoke.py dist/MoneyPrinterTurbo/MoneyPrinterTurbo` 验证打包后的启动、音色资源、素材导入、spawn 子进程、真实 MP4 合成和导出；Windows 路径使用 `.exe`。

GitHub Actions 的 **Desktop packages** 工作流可手动构建三种平台的包，并提供下载 artifact。这些是未经签名的开发包；Windows 签名、macOS 签名与公证、自动升级尚未实现。首次分发还需在目标机器验收 WebView、音视频编码和外部 API；系统 WebView 支持的预览格式可能比 FFmpeg 少。

## 三分钟验收

1. 启动程序，进入素材库导入一张图片 / 视频和一段音频。
2. 选择素材，回到创作工作台填写文案；配音选“上传音频”，选择导入音频，字幕先关闭，配乐选“无”。
3. 生成视频。确认进度变化、任务历史日志及最终 MP4 预览；点击“导出成片”另存为。
4. 新建任务并取消，确认本地工作进程退出；重开应用确认历史和设置保留。导出 / 导入预设后，重新选择本机素材。
5. 填写模型 / 配音 API 配置后再测试在线文案、音色和字幕。没有有效账号时应显示明确错误，不能将本地验收视作外部服务验收。

## 已知限制

- 桌面初版保留兼容工作台入口；素材搜索的专用交互、部分发布管理操作和密钥备份仍使用原工作台。
- 任务串行运行；关闭应用不在后台继续生成，也不自动恢复执行已中断的任务。
- 历史列表显示最近 100 条；SQLite 中较早记录继续保留。
- Whisper 首次下载、网络代理、服务额度和第三方 API 变更沿用原项目限制。桌面封装不会让在线 AI 服务变成离线服务。
