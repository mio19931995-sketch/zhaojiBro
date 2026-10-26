# Lulu 4.3.1 → Windows 0.2.0 同步记录

上游：[AidenXu-1/Lulu](https://github.com/AidenXu-1/Lulu)，tag `v4.3.1`，提交 `8ae31a76e1e135e6e016c46e270e9992c7162a51`。

Windows 基线为 `2dbed1efced19af4f442439797ddcf99dcd99aaa`，包含 PR #1 的重转录、可见选中批量操作、媒体缺失提示修复。同步时保留既有资料、模型、凭据、历史版本、媒体缓存和 Codex 读取接口。不自动下载模型权重，不运行实际直播抓取或付费 AI 请求。

## 证据与边界

- **E1 / 公开源码**：上游 `app/src/core/live_recording` 的协议处理、签名、弹幕记录与进程入口；用于分析跨平台边界。可移植协议保留上游许可证，Windows 子进程、任务持久化与 API 另外适配。
- **E2 / 公开源码**：`app/src/ui/agent_workspace/workspace.js`、编辑器与后端服务。用于核对文稿/对话并排、上下文选择、草稿保存、建议应用和版本管理。Windows 复用现有模型配置及凭据存储，采用 React/FastAPI 实现。
- **E3 / REA 静态构建产物分析**：REA CLI **6.7.0**，对 tag 中的 `app/src/ui/agent_workspace/assets/workspace.js` 执行 `analyze-javascript-application`。证据 ID `ev_ee49cffcf4cfef12c85f00927634c2d2a903748002096b217db8f87ef5be3490`；产物 SHA-256 `264a3f31455425426338603e76662b078edffe3a059c8ee825c2803c4d139800`。摘要见 [rea/upstream-workspace-evidence.json](rea/upstream-workspace-evidence.json)。完整记录仅保留在本地忽略的 `verification/rea/`。
- **E4 / 公开源码**：上游本地模型注册与运行器；核对 SenseVoice、Whisper Turbo、Whisper Turbo 量化版、MOSS 的模型格式和平台限制。Windows 后端映射、权重来源、实际运行检查见 [ASR_PORT.md](ASR_PORT.md)。

REA 分析没有执行 JavaScript，也没有运行 macOS 安装包。语义图受到节点/未知项预算限制，覆盖标为 **partial / truncated**；不能用它证明完整运行行为或全部安全性。业务结论另以相应源码交叉核对。没有把已有 Codex MCP 连接冒充为 REA MCP；本次使用已安装的 REA CLI。

## 功能映射

| 官方能力 | Windows 接入位置 | 验证说明 |
| --- | --- | --- |
| 抖音直播监控、分段录制、互动记录 | 工作台 → 直播录制 | 状态与进程通过隔离测试；真实平台、登录 Cookie 和断流恢复需真机验证 |
| 文稿 AI 工作区 | 工作台 → 文案处理 | 原稿/助手并排，草稿与会话持久化，建议确认后保存；测试使用模拟模型 |
| 四款本地模型 | AI 大模型 → 本地转录 | 使用 Windows 对应推理后端，运行器和权重状态分开显示；权重由用户主动导入/下载 |
| 界面简化 | 工作台、素材库、配置与连接 | 配音/切片归入可展开工具区；保留文稿、输出、音频、回收站与连接入口 |
| 已有 Windows 集成 | Codex / 飞书 / Obsidian / 历史版本 | 保留接口，不自动打开 Codex 项目、粘贴或发送 |

上游的 macOS MLX、XPC 和原版 `lulu-agent` 可执行文件不能直接运行在 Windows。这里同步功能与数据流程，不声称二进制等同、模型数值完全一致或已验证全部官方操作。新模型的硬件速度、识别质量与分发许可按实际模型说明核对。

## 复现与验证

所有自动化使用独立 `LULU_DATA_DIR`，不读写用户资料；生产数据与模型保持不动。具体模块测试、运行器检查与仍需用户账户/真实平台的项目见 `AI_WORKSPACE_PORT.md`、`ASR_PORT.md`、`LIVE_PORT.md` 和本次 PR 描述。

2026-10-25 完成的验证：

| 命令或实测 | 结果 | 边界 |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe -m pytest tests -q` | 185 passed；1 条现有 Starlette 弃用提示 | 含 AI 14、直播 17、ASR 16 项；云端与平台响应使用替身 |
| `node tests/creator-workflow.mjs` | 13 条流程通过 | Edge 与隔离后端，合成音视频 |
| `node tests/review-workflow.mjs` | 8 条流程通过 | JEV 使用离线响应，保留原检查流程 |
| `node tests/codex-mcp.mjs` | 通过 | 真实 MCP SDK、只读库、FFmpeg 视频帧；不含指定 Codex 项目续接 |
| `npm run test:desktop` | 5 组跨模块检查通过，页面异常 0 | 实际 Windows Electron 与本地 HTTP 模型替身；发送区在窗口内、暗色模型卡片可用 |
| 原生转录运行工具 | DLL 0.4.0、CPU ABI 可用；发布包摘要匹配 | 没有下载模型权重，不能据此证明识别质量 |
| 录制进程 | 真实 FFmpeg 合成流分段、封装、父管道关闭收尾 | 不含真实抖音直播与认证 |
| 退出清理 | 自有原生父子/孙进程在 8 秒预算内退出 | Electron 为后端提供 20 秒收尾窗口，直播与转录并行停止 |
| `npm run build` / `git diff --check` | 通过 | 生产前端已重新构建 |

2026-10-26 的第二阶段已适配片段引用、局部处理、逐项建议采纳/忽略及最近采纳撤销，见 [第二阶段记录](AGENT_SYNC_PHASE2.md)。第三阶段继续适配自动上下文压缩、16 项应用工具及技能管理，见 [第三阶段记录](AGENT_SYNC_PHASE3.md)。Windows 的逐项操作先修改工作区草稿，再显式保存到原文；工具持久写入先展示待确认卡。JEV 原有检查继续保留，官方严格校对硬校验尚未迁移。
