param([string]$InputJson)
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Speech
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime] > $null
[Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType=WindowsRuntime] > $null

function Await-WinRt([object]$Operation, [type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    return $task.GetAwaiter().GetResult()
}

function Get-VoiceList {
    $oneCore = @([Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices)
    $result = @($oneCore | ForEach-Object {
        @{
            id = 'onecore:' + $_.Id
            name = $_.DisplayName
            language = $_.Language
            gender = if ([int]$_.Gender -eq 0) { 'male' } else { 'female' }
            engine = 'onecore'
        }
    })

    $desktop = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $oneCoreNames = @($oneCore | ForEach-Object { $_.DisplayName.ToLowerInvariant() })
        $result += @($desktop.GetInstalledVoices() | Where-Object {
            $baseName = $_.VoiceInfo.Name -replace ' Desktop$', ''
            $oneCoreNames -notcontains $baseName.ToLowerInvariant()
        } | ForEach-Object {
            @{
                id = 'desktop:' + $_.VoiceInfo.Name
                name = $_.VoiceInfo.Name
                language = $_.VoiceInfo.Culture.Name
                gender = $_.VoiceInfo.Gender.ToString().ToLowerInvariant()
                engine = 'desktop'
            }
        })
    } finally {
        $desktop.Dispose()
    }
    return @($result | Sort-Object @{Expression={ if ($_.language -eq 'zh-CN') { 0 } else { 1 } }}, name)
}

function Write-OneCoreSpeech($Request, [string]$VoiceId) {
    $voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices |
        Where-Object { $_.Id -eq $VoiceId -or $_.DisplayName -eq $VoiceId } |
        Select-Object -First 1
    if (-not $voice) { throw '找不到所选 Windows 音色' }

    $synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
    $stream = $null
    $reader = $null
    try {
        $synth.Voice = $voice
        $escaped = [System.Security.SecurityElement]::Escape([string]$Request.text)
        $percent = [Math]::Max(50, [Math]::Min(200, 100 + ([int]$Request.rate * 10)))
        $ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='$($voice.Language)'><prosody rate='$percent%'>$escaped</prosody></speak>"
        $operation = $synth.SynthesizeSsmlToStreamAsync($ssml)
        $stream = Await-WinRt $operation ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
        $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
        $loaded = Await-WinRt ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
        $bytes = New-Object byte[] $loaded
        $reader.ReadBytes($bytes)
        [System.IO.File]::WriteAllBytes([string]$Request.output, $bytes)
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        $synth.Dispose()
    }
}

if (-not $InputJson) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    ConvertTo-Json -InputObject @(Get-VoiceList) -Compress
    exit
}

$request = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
$selected = [string]$request.voice
$oneCoreVoice = $null
if ($selected.StartsWith('onecore:')) {
    $oneCoreVoice = $selected.Substring(8)
} elseif ([Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices.DisplayName -contains $selected) {
    $oneCoreVoice = $selected
}

if ($oneCoreVoice) {
    Write-OneCoreSpeech $request $oneCoreVoice
} else {
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        if ($selected.StartsWith('desktop:')) { $selected = $selected.Substring(8) }
        if ($selected) { $synth.SelectVoice($selected) }
        $synth.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$request.rate))
        $synth.SetOutputToWaveFile($request.output)
        $synth.Speak($request.text)
    } finally {
        $synth.Dispose()
    }
}
