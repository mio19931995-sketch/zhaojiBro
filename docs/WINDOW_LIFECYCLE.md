# Windows 关闭与重复启动修复

2026-10-27：在独立资料库的 Windows Electron 中，先销毁主窗口，再触发原有 `second-instance` 处理器，复现 `Object has been destroyed`，堆栈与反馈一致，指向修改前的 `electron/main.cjs:20:62`。

原因是原处理器仅检查窗口变量是否有值，窗口关闭后仍引用已经销毁的对象。正常退出还可能遇到另一处竞态：在关闭服务的 HTTP 请求结束后才监听子进程退出；如果子进程已退出，就可能空等完整的 20 秒。

## 修复行为

- 主窗口关闭后清空引用并进入退出流程。重复启动、全屏事件、权限回调、页面加载结束与 IPC 入口检查窗口和退出状态。窗口 `closed` 后清理引用遵循 [Electron 窗口说明](https://www.electronjs.org/docs/latest/api/browser-window#event-closed)。
- 应用正常运行时，重复启动仍恢复并聚焦现有窗口；退出中再次启动，只安排一次待清理完成后的原生重开。使用 [Electron relaunch](https://www.electronjs.org/docs/latest/api/app#apprelaunchoptions)，保留用户原本打开应用的意图。
- 多次退出请求共用同一清理过程，不能提前跳过后台收尾；监听退出事件在请求关闭服务之前建立，已退出的进程直接通过，并清理计时器和事件监听。
- 保留现有 20 秒后台收尾预算及仅针对本实例拥有的进程树的超时终止。没有修改正式资料库、模型、API 配置或转录逻辑。

## 验证

```powershell
node --test tests/electron-process-exit.cjs
node tests/electron-lifecycle.mjs
node tests/wechat-workflow.mjs
```

- 4 项进程退出测试通过：已退出/信号退出/未成功启动；HTTP 响应前退出；响应后退出；超时与监听清理。
- 4 类真实 Windows Electron 检查通过：真实第二实例恢复最小化窗口；已关闭窗口的迟到回调、重复启动与退出；原生重开后的可见窗口、旧服务退出及文稿保留；正常关闭不空等完整宽限期。
- 公众号功能的 4 类本地桌面检查通过，0 页面错误。

退出竞态测试将请求延迟 2 秒，并模拟触发退出阶段的重复启动事件，稳定覆盖窗口已销毁但进程尚未退出的状态。随后实际执行 Electron 重开；新窗口、新 Python 服务和文稿保留均实测。重开测试只移除 Playwright 调试启动参数。测试全部使用独立目录和配置，不关闭用户其他应用。证据保存在忽略目录 `.test-data/electron-lifecycle-*`。

此次未重新运行 Python 全量测试或平台网络抓取；Python 业务与前端产物未修改。多小时直播、系统关机/注销及设备驱动异常仍需对应场景验证。
