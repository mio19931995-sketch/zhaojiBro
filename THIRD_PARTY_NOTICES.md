# 来源与第三方组件

本项目是根据 [Lulu](https://github.com/AidenXu-1/Lulu) 公开功能开发的 Windows 应用，不是原作者提供的 Windows 发行版。0.2.0 起依据官方 v4.3.1 开源实现适配直播协议与工作流程；复用模块保留来源与许可证，其他界面和 Windows 任务管理按本项目技术栈实现。不分发原版安装包或商业授权系统。

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

依赖通过安装步骤获取，FFmpeg 由使用者另外安装，语音模型仅在用户明确操作后下载或导入；本仓库不包含这些运行文件和模型权重。

直播协议、签名与弹幕适配来自官方 Lulu v4.3.1（提交 `8ae31a76e1e135e6e016c46e270e9992c7162a51`）。其 MIT 许可及继承的第三方声明随相应模块保存，具体文件与改动见 `docs/LIVE_PORT.md`。本项目保留官方来源版权声明，不将第三方代码改用本仓库其他许可。界面色彩与功能组织参考官方公开主题；没有复制官方吉祥物或商业授权资源。

`backend/agent-skills/packages/` 的六项内置技能 Markdown 与目录元数据来自同一 Lulu v4.3.1，保留原内容与 `backend/agent-skills/LICENSE.Lulu` 的 MIT 许可。Windows 技能包管理和封闭工具集参照公开数据流程实现；技能包保存为本地 SQLite 数据，不使用 macOS 文件锁或执行包内脚本。
