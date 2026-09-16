# 来源与第三方组件

本项目根据 [Lulu for macOS](https://github.com/AidenXu-1/Lulu) 的公开功能介绍和操作演示独立实现，不是原作者提供的 Windows 发行版，未包含原版应用源代码、安装包或商业授权系统。

公开仓库中的 `public/app-icon.svg` 和由它渲染的 `public/app-icon.png` 为本项目独立绘制的文档与声波图形。原版吉祥物、原版图标、参考录屏及截图不随本仓库分发。

本仓库暂未指定代码的开源许可证。公开访问不改变各第三方组件的许可条件；如需再分发，请核对所使用版本附带的许可文件。

主要组件：

- [Electron](https://github.com/electron/electron)
- [React](https://github.com/facebook/react)
- [Vite](https://github.com/vitejs/vite)
- [Phosphor Icons](https://github.com/phosphor-icons/react)
- [FastAPI](https://github.com/fastapi/fastapi)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [OpenCC Python](https://github.com/yichen0831/opencc-python)
- [CTranslate2](https://github.com/OpenNMT/CTranslate2)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FFmpeg](https://ffmpeg.org/)

依赖通过安装步骤获取，FFmpeg 由使用者另外安装，语音模型首次使用时按需下载；本仓库不包含这些运行文件和模型权重。
