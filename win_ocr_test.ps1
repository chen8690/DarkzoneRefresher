$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

Write-Output "=== AvailableRecognizerLanguages ==="
foreach ($l in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    Write-Output ("  " + $l.LanguageTag + "  " + $l.DisplayName)
}

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

# 图片路径：优先命令行第 1 个参数，否则按脚本自身位置顺次找
$imgPath = $args[0]
if (-not $imgPath) {
    foreach ($cand in @(
        (Join-Path $PSScriptRoot "ocr_test.png"),      # make_ocr_test.py 的产物
        (Join-Path $PSScriptRoot "samples\live_list.png"),
        (Join-Path $PSScriptRoot "samples\live_detail.png")
    )) {
        if (Test-Path $cand) { $imgPath = $cand; break }
    }
}
if (-not $imgPath -or -not (Test-Path $imgPath)) {
    Write-Output "找不到待识别图片；用法: .\win_ocr_test.ps1 <图片路径>（或先跑 make_ocr_test.py 生成 ocr_test.png）"
    exit 2
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $lang = New-Object Windows.Globalization.Language "zh-Hans-CN"
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    Write-Output "engine from explicit zh-Hans-CN"
}
if ($null -eq $engine) { Write-Output "NO OCR ENGINE"; exit 1 }
Write-Output ("engine language: " + $engine.RecognizerLanguage.LanguageTag)

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
Write-Output "=== Recognized lines ==="
foreach ($line in $result.Lines) {
    Write-Output ("  [" + $line.Text + "]")
}
