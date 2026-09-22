param([string]$DataDirectory = '')
$ErrorActionPreference = 'Stop'
$luluNode = Get-Command node.exe -ErrorAction Stop
$luluCodex = Get-Command codex.cmd -ErrorAction SilentlyContinue
if (-not $luluCodex) { $luluCodex = Get-Command codex.exe -ErrorAction SilentlyContinue }
if (-not $luluCodex) { throw '未找到 Codex CLI，请安装 Codex CLI 后重试。' }
$luluServer = Join-Path $PSScriptRoot 'mcp\lulu.cjs'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules\@modelcontextprotocol\sdk'))) { throw '请先运行 setup.ps1 安装依赖。' }
if (-not $DataDirectory) { $DataDirectory = Join-Path $PSScriptRoot 'data' }
$luluData = [System.IO.Path]::GetFullPath($DataDirectory)
# Modify only the named server through Codex's own configuration command.
& $luluCodex.Source mcp add lulu --env "LULU_DATA_DIR=$luluData" -- $luluNode.Source $luluServer
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP 配置失败，请检查 Codex CLI。' }
Write-Output 'Lulu 连接配置已保存；重新打开 Codex 后可使用素材读取工具。'
