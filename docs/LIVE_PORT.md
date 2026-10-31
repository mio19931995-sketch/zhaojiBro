# Windows 直播工作区 · Lulu 4.3.1 对齐记录

## 来源与范围

固定官方 tag **v4.3.1**，commit `8ae31a76e1e135e6e016c46e270e9992c7162a51`。
参照源码位于 `reference/upstream-lulu-4.3.1`（只读参考，不随发布打包）。

| 官方证据 | Windows 实现 |
| --- | --- |
| `app/src/core/live_recording/douyin.py` 的 `DouyinProvider.check` / `parse_room` / `room_metrics` | 复制公开直播间解析协议；区分未开播、受限、需要登录、网络失败；在线人数、累计观看与点赞字段不混用，约数保留原文 |
| `app/src/core/live_recording/ab_sign.py` | 复制官方公开请求签名实现，无硬编码账号或凭据 |
| `app/src/core/live_recording/contracts.py` 的 `public_link` / `safe_stream_url` | 复用固定抖音来源域名与媒体 CDN 白名单；本机录制参数不接受任意 URL 或用户指定输出路径 |
| `app/src/core/live_recording/danmaku.py` | 复制 WebSocket / gzip / protobuf / ack / 去重 / 重连协议，保留 biliup 署名和许可 |
| `app/src/core/live_recording/comments.py` | 原实现包含 Unix 文件所有权检查，改为 Windows 可用的受管目录、追加日志、截断尾保护和 256 MiB 上限 |
| `app/src/core/live_recording/media.py` 的 `SegmentProducer` | 保留 TS 分段策略；用 `live_guard.py` 重写父管道 EOF 守护，避免 Unix 进程组依赖 |
| `app/src/ui/live*` | 以现有 React 工作台重新呈现直播间、录像、统计、弹幕和恢复入口；不复制 macOS 原生 IPC |

复制的 4 个协议模块放在 `backend/vendor/live/`，同目录保留 `LULU_LICENSE.txt`、
`STREAMGET_LICENSE.txt`、`BILIUP_LICENSE.txt` 及 `provenance.md`。

## 已实现的使用路径

1. 直播监控页粘贴抖音直播间/主播主页/公开分享链接，选择分段时长及是否保存弹幕。
2. 添加只保存列表；「检查一次」不录制；「监控并录制」才开始每分钟检查并在开播时录制。
3. 同时最多 3 位主播，最多保存 100 位主播配置。需登录时复用现有平台登录窗口产生的个人 Cookie。
4. 录像先保存为 TS，已关闭的分段封装为 MP4 后可回放、下载、加入素材库转录或交给 Codex。
5. 录像页可查看最近互动样本、搜索/分页弹幕、导出完整 UTF-8 CSV；缺失统计保持空白/「未提供」。
6. 「暂停」停止监控并保存最后一段；移除主播不会删除历史录像。
7. 程序退出先关闭本次拥有的录制守护管道；总等待最多 12 秒。退出阶段保留原始分段，避免长时间封装阻塞退出。
8. 重启将遗留活动记录标记为中断，**不自动恢复网络或录制**；「恢复分段」可对保留 TS 补做封装。

资料存于现有 `LULU_DATA_DIR/live/recordings/<内部ID>`，SQLite `objects` 保存
`live_host` / `live_recording` 元信息。不会更改旧文稿、模型或全局下载保留默认。
下载/导出的目的位置仍由用户的桌面下载流程决定。

## 运行依赖与集成

- `requests`、`websocket-client`（无模型下载）；已有 FFmpeg 与 FFprobe。
- FastAPI `app.include_router(live.router)`，lifespan 调用同步 `live.startup()` 和 `await asyncio.to_thread(live.shutdown)`。
- React `LiveWorkspace` 使用现有 `Shared`；导入分段后 `navigate('extract')` 并选择素材。
- 本机 API 沿用既有 origin/client 校验；本模块不开放远端监听或外部写入工具。

## 验证

命令：`.venv\Scripts\python.exe -m pytest tests/test_live_workspace.py -q`。
本轮结果 **17 passed**；`npx tsc --noEmit` 通过。
所有测试在导入 backend 前指定临时 `LULU_DATA_DIR`，每例重建临时 SQLite，
禁止 `requests.Session.request` 访问真实平台。

覆盖：公开链接白名单、约数/未知人数、明确离线判定、仅检查不录制、探测取消防止晚到结果启动录制、
监控到录像与统计持久化、网络尚未返回时有界退出、重启不自动录制、弹幕 gzip/protobuf/ack、
分页搜索、CSV 公式前缀保护、日志截断尾与容量边界、录制进度不覆盖已导入素材关联。
本地真实 FFmpeg 合成测试覆盖 TS 多分段 → MP4 封装 → Range 播放响应 → 素材库幂等导入 →
恢复不重复分段，以及父管道 EOF 后 FFmpeg 退出且媒体可读。

Windows 实测发现 Python 守护程序在 FFmpeg 自然结束时仍有阻塞 stdin 线程，解释器销毁会异常退出；
现以已清理 FFmpeg 后的 `os._exit` 结束独立 helper，并由同一合成回归验证正常退出。

## 待真机验证与已知边界

- **真实抖音直播、登录状态、平台访问限制、真实弹幕与直播断线重连：待平台真机验证。** 本轮未执行直播采集、录制或认证请求。
- 官方协议也依赖平台当前行为；返回受限/未连接时显示状态，不模拟成功、不绕过平台验证。
- Windows Electron 真实交互与跨模块验收由主任务执行；本模块 Python 测试不替代 UI 验证。
- 复制封装不转码；优先选 H.264 源。少数直播编码可能无法在 Electron 播放，源 TS 和下载 MP4 均保留。
- 录制需要应用运行；没有 Windows 系统后台服务或开机自动启动。退出后只保留现有结果。
- 互动记录是平台间隔采样，不承诺每个点赞变化均有逐事件数据；弹幕仅记录成功连接之后收到的公开消息。
- 大分段封装失败保留原文件并提示恢复；不自动删除历史录像，也不自动下载转录模型。
