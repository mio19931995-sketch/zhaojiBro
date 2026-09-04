param([string]$InputJson)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if (-not $InputJson) {
        $installedVoices = @($synth.GetInstalledVoices() | ForEach-Object { @{ name=$_.VoiceInfo.Name; language=$_.VoiceInfo.Culture.Name; enabled=$_.Enabled } })
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        ConvertTo-Json -InputObject $installedVoices -Compress
    } else {
        $request = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($request.voice) { $synth.SelectVoice($request.voice) }
        $synth.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$request.rate))
        $synth.SetOutputToWaveFile($request.output)
        $synth.Speak($request.text)
    }
} finally {
    $synth.Dispose()
}
