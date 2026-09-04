# Lulu Workbench

面向 Windows 的本地内容工作台：把音视频转换为文稿，整理素材，再进行文案处理、系统配音、切片与知识库导出。

这是参考 [Lulu for macOS](https://github.com/AidenXu-1/Lulu) 的公开工作流程独立实现的开发预览版，并非官方 Windows 发行版。当前分享的是源码项目，尚无双击即装的独立安装包。界面采用本项目独立绘制的图标。

> 使用前请查看 [已知问题](docs/KNOWN_ISSUES.md)。本地核心流程已验证；飞书真实账号写入、部分平台采集与字幕仍需验证，不承诺全部功能或全部链接可用。

## 界面预览

![Lulu Workbench 工作台](docs/images/workbench.png)

## 在 Windows 上安装

支持 Windows 10 / 11，当前验证环境为 x64。第一次安装需要联网下载依赖和语音模型。建议将源码解压到普通用户有写入权限的文件夹，例如 `D:\Apps\LuluWorkbench`。

1. 从本仓库页面点击 **Code → Download ZIP**，解压到一个固定目录；也可以用 Git 克隆。
2. 安装以下依赖，并重新打开终端，让 PATH 生效：
   - [Node.js](https://nodejs.org/en/download)：22.12 或更新版本，包含 npm。
   - [uv](https://docs.astral.sh/uv/getting-started/installation/)：用于自动准备 Python 3.12 和 Python 依赖。
   - [FFmpeg](https://ffmpeg.org/download.html)：Windows 版本，确保终端可运行 `ffmpeg -version` 与 `ffprobe -version`。
3. 在项目文件夹打开 PowerShell，运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

此命令仅为本次安装脚本设置执行策略，不修改系统的全局执行策略。脚本会检查依赖、创建项目内 Python 环境、安装锁定依赖并构建界面。重复运行会复用已有 Python 环境，不删除用户资料库。

4. 安装完成后，双击 **start.vbs**。也可以在项目目录运行 `npm.cmd start`。
5. 首次打开进入 **AI 大模型 → 本地语音转录**，下载 **Whisper Base**。模型下载完成后，即可导入音视频并点击任务的“开始”。

需要创建桌面快捷方式时，右键 `start.vbs`，选择“发送到 → 桌面快捷方式”。启动依赖项目目录，请保留 `.venv`、`node_modules`、`dist` 和 `backend`；不要只复制 `start.vbs` 到另一台电脑。

## 能做什么

- 导入本地音视频、TXT、Markdown 和 SRT；支持拖放和录音入口。
- faster-whisper 本地 CPU / INT8 转录、任务队列、进度、暂停和失败重试。
- 文稿正文与逐段时间轴编辑、文稿及文案处理草稿恢复。
- 文件夹管理、搜索、Shift 连选、批量移动和导出。
- TXT、Markdown、SRT、CSV、ZIP 导出与 Obsidian Markdown 导出。
- 抖音独立登录窗口与主页作品采集；其他公开链接通过 yt-dlp 处理。
- 独立下载封面、音视频，优先使用可获取的字幕，选择保留目录和定位本地文件。
- 接入 OpenAI Chat Completions 兼容文本服务，进行摘要、校正、改写、大纲和翻译。
- 使用 Windows 系统语音生成 WAV 配音，按起止时间截取本地音视频。
- 飞书 OAuth、知识库位置预设、新建或已有多维表格、字段及附件导出和失败重试。
- 中文界面、浅色与深色外观。

音色克隆、实时直播录制、自动精彩片段识别、自动更新和独立安装器尚未实现。“直播切片”页面目前处理的是已导入的本地录播文件。

## 常用操作

1. **转录**：文案提取 → 导入文件或粘贴链接 → 开始 → 查看右侧文稿。
2. **校对**：在正文页编辑后保存；需要保留字幕时间轴时，请在“时间轴”逐段修改。全文替换会清除旧时间戳。
3. **整理与导出**：在文稿库按文件夹、多选或搜索整理，再导出文件或存入 Obsidian。
4. **文本模型**：在“AI 大模型 → 文本处理模型”填写实际可用的基地址、模型名称和必要的 API Key。例如基地址为 `http://127.0.0.1:11434/v1`，程序会追加 `/chat/completions`。本项目不附送模型服务或 API 额度。
5. **配音**：选择已保存文稿或输入正文，选系统声音和语速。音色取决于 Windows 已安装的语音包。
6. **飞书**：填写自己的应用信息，按页面配置权限和三个回调地址，再登录授权并设置导出位置。真实写入需要对应账号有访问权限。

链接采集取决于平台的正常登录状态、网络和内容权限。请在程序自己的窗口中完成平台要求的登录或验证。转录结果中的同音字、人名和专有名词仍需校对。

## 数据存储与备份

- `data/library.sqlite3`：文稿、任务、草稿与设置。
- `data/media/`：导入和下载的音视频。
- `data/models/`：语音模型。
- `data/outputs/`：生成的配音与切片。

关闭程序后备份整个 `data` 文件夹。移除列表记录不会自动删除源文件；目前没有界面回收站。关闭主窗口会结束此次启动的本地服务，未完成任务下次打开可重新开始，已下载媒体可复用。

App Secret 与模型 API Key 使用 Windows DPAPI 加密，迁移到其他 Windows 用户或电脑后需要重新配置。平台 Cookie 和独立登录窗口的会话用于用户主动发起的采集；程序不会读取现有 Chrome/Edge 账号配置。

本地转录在本机完成；模型下载和内容采集会连接对应服务。使用文本模型会把所选正文发给配置的服务，飞书导出会把用户选择的资料发送至飞书。

本仓库不包含用户数据库、音视频、账号密钥、Cookie、模型权重、参考录屏或本地验收截图。

## 开发与验证

完成安装后：

```powershell
npm.cmd run build
npm.cmd start
```

浏览器开发需要两个终端，分别运行：

```powershell
.\.venv\Scripts\python.exe -m backend.main
npm.cmd run dev
```

浏览器后端默认为 `127.0.0.1:18793`；桌面程序选择空闲回环端口。可使用 `LULU_DATA_DIR` 指定独立资料库目录。

可重复的后端回归测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_backend.py tests/test_parity.py tests/test_acceptance.py -q
```

这些测试使用独立临时资料库，云端交互使用测试响应，不能代表真实账号写入或每个平台都已验证。本地个人验收脚本和原始报告没有纳入公开仓库；已知问题统一记录在 [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)。

## 来源与许可

交互流程参考 Lulu 的公开介绍与操作演示，Windows 程序独立实现。第三方依赖及图标来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库暂未指定代码的开源许可证，各依赖继续遵循其自身许可。
