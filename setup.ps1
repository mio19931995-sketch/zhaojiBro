$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw '此项目目前只支持 Windows。' }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw '请先安装 uv（https://docs.astral.sh/uv/），然后重新运行。' }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw '请先安装 Node.js 22.12 或更新版本（https://nodejs.org/）。' }
$luluNodeVersion = [version]((& node --version).TrimStart('v'))
if ($luluNodeVersion -lt [version]'22.12.0') { throw '需要 Node.js 22.12 或更新版本。' }
foreach ($luluMediaTool in @('ffmpeg', 'ffprobe')) {
    if (-not (Get-Command $luluMediaTool -ErrorAction SilentlyContinue)) {
        throw "找不到 $luluMediaTool。请安装含 ffmpeg.exe 和 ffprobe.exe 的 FFmpeg，并将 bin 文件夹加入 PATH 后重新打开终端。"
    }
}
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    if (Test-Path -LiteralPath '.venv') { throw '.venv 已存在但不完整，请先将它备份到其他名称后重试。' }
    uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败' }
}
uv pip install --python .venv/Scripts/python.exe -r backend/requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
npm.cmd ci
if ($LASTEXITCODE -ne 0) { throw 'Node 依赖安装失败' }
node node_modules/electron/install.js
if ($LASTEXITCODE -ne 0) { throw 'Electron 运行文件下载失败' }
npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw '界面构建失败' }
Write-Output '安装完成，双击 start.vbs 启动。请在 AI 大模型页面下载转录模型。'
