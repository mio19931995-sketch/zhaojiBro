# Lulu 4.3.1 本地转录：Windows 适配说明

对照基线：`AidenXu-1/Lulu@8ae31a76e1e135e6e016c46e270e9992c7162a51`（v4.3.1）。
读取 `app/src/core/engines/local_models.py`、`local_native.py`、`native_process.py`、`app/packaging/build_asr_runtime.py` 后确认：上游四款引擎都标注 Apple Silicon；MLX 和 Metal 不能直接移植到 Windows。

## 实际对应关系

| 上游功能 | Windows 入口 / 实际后端 | 时间轴 | 发言人 | 权重复用范围 |
| --- | --- | --- | --- | --- |
| SenseVoice Q8 | SenseVoice Small Q8 / transcribe.cpp CPU | 无 | 无 | 需要 handy-computer 的 GGUF，不能混用上游 FunAudioLLM GGUF |
| Whisper MLX Turbo | 原有 Whisper Large v3 Turbo / CTranslate2 CPU INT8 | 分段 | 无 | 继续使用已有 faster-whisper 权重 |
| Whisper MLX Turbo 4bit | Whisper Turbo Q4_K_M / transcribe.cpp CPU | 分段 | 无 | 需要 Windows GGUF；不把 MLX 4bit 文件标为兼容 |
| MOSS Q5 | MOSS Q5 / transcribe.cpp CPU | 分段 | 匿名正整数 ID | 与上游同一 MOSS GGUF，SHA-256 相同 |

原有 Tiny / Base / Small、默认模型和资料不会迁移或删除。四款对应入口体现相同用途，但不承诺 MLX 与 Windows 量化实现的速度、内存和识别质量一致。

## 使用方式

1. 进入 AI 大模型 → 本地语音转录。原有模型仍可直接选择。
2. 新模型共用 Windows x64 原生工具。点击「准备运行工具」只下载约 18 MB 的 DLL 运行包，不下载模型；如果 Python 环境已有 `transcribe-cpp-native`，则先验证并复用。也可以手动导入含 `transcribe.dll` 与配套 DLL 的目录。
3. 从「查看模型来源与文件」核对所需文件，在「导入已有模型」粘贴本地路径或选所在目录。需要用户自行准备模型文件。本轮开发没有下载任何模型权重。
4. 导入只建立引用，不移动或复制权重。校验文件大小、GGUF 标识和固定 SHA-256；不匹配时保留上一个有效绑定。文件修改/移动后会失去可用状态，重新校验即可。
5. 原有 Whisper 可手动引用其他位置的 CTranslate2 模型目录，使用 `local_files_only=True` 实际加载验证。已知缓存目录的发现是只读的，发现后需要点击「校验并复用」。
6. 模型文件和运行接口都校验后才允许选择新增模型。之后通过已有本地导入、链接转录或重转录入口使用。移除引用不会删除外部权重。

## 数据与限制

- SenseVoice 原生模型不提供精确时间戳。长音频先按约 25 秒分块，单进程只加载一次模型，逐块处理；不伪造字幕或发言人轴。断句处识别效果待真实音频验证。
- MOSS 支持中文/英文，语言设置为日语/韩语时直接说明不支持。长音频按上下文估算预检，超过上限提示先切片，不截断源音频。CPU 耗时和内存占用待真机实际权重验证。
- 中文输出通过原有 OpenCC 统一为简体，英文保持英文。匿名发言人存储为 `segments[].speaker_id: number`；Markdown 导出保留发言人，SRT 保持标准正文。
- `metadata.asr.timestamp_kind` 为 `none` 或 `segment`，`timing_estimated=false`。不使用均摊时长生成假时间轴。
- MOSS 校验原始 `[时间][S编号]正文[结束时间]` 与返回段落，拒绝缺尾标记、错误发言人、超出音频时长或缺字结果。可恢复文本保存在 `metadata.asr_partial_text`，不会标记完成或覆盖现有文稿。
- 原生 DLL 在独立 Python 子进程运行，CPU 后端明确指定。取消时停止该子进程树（包括 Windows venv 启动器的子 Python），输出大小和执行时间有限制。应用退出由 `engine.shutdown(timeout=8)` 取消排队任务，并在共享的 8 秒预算内关闭已登记的原生子进程树；退出期间不会启动新子进程。
- 默认不启动任何模型下载。原有 Whisper 的「下载模型」按钮仍是用户主动操作。新增三个模型没有权重下载代码。

## Windows 运行证据

- 运行包：`transcribe-native-0.4.0-windows-x86_64-cpu-vulkan.tar.gz`，18,036,555 bytes。
- 发布资产 SHA-256：`9d5bd89f720d7979323e73f5220fe50fad5e38a7b0c2525225d453bd09adf2b0`，本机下载完成后实际匹配。
- 该发布包只有 DLL，不包含 `transcribe-cli.exe`。所以实现通过 `transcribe-cpp==0.4.0` 官方 Python API 调用，避免假设不存在的 CLI。
- 本机 Windows x64 DLL ABI 加载验证：`native_version=0.4.0`，`native_commit=f1dcd15`，`backend_available('cpu')=True`。枚举出 CPU 和 Intel Iris Xe Vulkan，本实现明确选择 CPU。
- `transcribe-cpp` 是 MIT 的纯 Python 绑定；依赖 `transcribe-cpp-native==0.4.0.*`（原生工具，无模型权重）。没有安装编译器或 CUDA 工具链。
- 自动化：`python -m pytest tests/test_backend.py tests/test_workbench_review.py tests/test_asr_windows.py -q` → 40 passed。新增 16 项测试覆盖完整性、只读发现、取消进程、真实 PCM 输入、匿名发言人、拒绝不完整输出、简体落盘及原有转录回归。退出回归启动真实合成 Python 父子进程，通过 Windows 进程句柄确认孙进程已终止，并检查退出耗时小于 8 秒。
- `npm run build` 通过。上面的模型调用测试采用伪模型和合成 PCM，用于验证接口与生命周期；不能替代真实语音准确率测试。
- **待真机验证**：三个新增 GGUF 模型的实际识别、MOSS 发言人准确率/内存上限、SenseVoice 长视频分块衔接、Windows Electron 用户完整操作。因为本机未准备这些权重，未自动下载，也未声称这些项目已经跑通。

## 固定权重校验

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| SenseVoiceSmall-Q8_0.gguf | 252684608 | `6c759ee4c9748c9b3f7a5a60ca74f0f7e685fb9d45d1378fce7cfd62f59adf29` |
| whisper-large-v3-turbo-Q4_K_M.gguf | 536069696 | `c6e172d0074f0fa9440b88247854b50643cd4bb58b8515c797bbed16d858c7df` |
| MOSS-Transcribe-Diarize-Q5_K_M.gguf | 700314304 | `cc36d8ad46c66a55125cd9339dc063e62c657d63de60d571442d19aea87e7349` |

这些摘要通过模型仓库公开 LFS 元数据核对，未请求权重文件。模型定义固定摘要，不自动跟随远端更新。

## 一手来源

- [Lulu 4.3.1 模型注册](https://github.com/AidenXu-1/Lulu/blob/8ae31a76e1e135e6e016c46e270e9992c7162a51/app/src/core/engines/local_models.py)
- [Lulu 原生引擎及 MOSS 完整性校验](https://github.com/AidenXu-1/Lulu/blob/8ae31a76e1e135e6e016c46e270e9992c7162a51/app/src/core/engines/local_native.py)
- [transcribe.cpp v0.4.0 Windows 发布资产](https://github.com/handy-computer/transcribe.cpp/releases/tag/v0.4.0)
- [Python API 与取消语义](https://github.com/handy-computer/transcribe.cpp/blob/10eefba7e33d6a2fcbed0b6af82c752fe0369b42/bindings/python/README.md)
- [Windows SenseVoice 模型](https://huggingface.co/handy-computer/SenseVoiceSmall-gguf/tree/main)
- [Windows Whisper Turbo 模型](https://huggingface.co/handy-computer/whisper-large-v3-turbo-gguf/tree/main)
- [MOSS 模型](https://huggingface.co/handy-computer/moss-transcribe-diarize-gguf/tree/main)

## 上游许可声明

MOSS 完整性检查借鉴 Lulu 的 MIT 实现，保留其许可：

```text
MIT License

Copyright (c) 2026 兆基 (AidenXu-1)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

原生工具包与 Python 绑定自带 `Copyright (c) 2026 The transcribe.cpp authors` 的 MIT 许可及 ggml 等第三方声明。安装器完整保留包内 `licenses/`，未把 DLL 或模型提交到 Git。
