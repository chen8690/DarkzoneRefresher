param([string]$Lang = "zh-Hans-CN")

# 常驻 OCR worker（2026-10-11 提速）：把 WinRT 引擎和 powershell 进程启动成本只付一次。
#
# 现状问题：一次性调 powershell.exe -File win_ocr.ps1 每次都要重新付「进程启动 + 引擎加载」
# 约 0.6 秒；实测 160x72 的小图也要 0.54 秒，全屏 0.87 秒 —— 说明耗时基本与面积无关。
# 改成常驻后：全屏 0.27 秒、右面板 0.13 秒、右面板三行 0.07 秒（中位，本机实测）。
#
# 协议（一行一个请求，一行一个回答）：
#   请求：<图片路径>\t<输出json路径>
#   回答：READY <语言>（启动时一次）/ OK <行数> / ERR <原因>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$language = New-Object Windows.Globalization.Language $Lang
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "no OCR engine available" }
$utf8 = New-Object System.Text.UTF8Encoding($false)

[Console]::Out.WriteLine("READY " + $engine.RecognizerLanguage.LanguageTag)
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim() -eq "") { continue }
    $parts = $line -split "`t"
    if ($parts.Count -lt 2) { [Console]::Out.WriteLine("ERR bad request"); [Console]::Out.Flush(); continue }
    $img = $parts[0]
    $out = $parts[1]
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $img).Path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

        $lines = New-Object System.Collections.ArrayList
        foreach ($l in $result.Lines) {
            $words = New-Object System.Collections.ArrayList
            foreach ($w in $l.Words) {
                $r = $w.BoundingRect
                $null = $words.Add([ordered]@{
                    text = $w.Text
                    x = [math]::Round($r.X, 1); y = [math]::Round($r.Y, 1)
                    w = [math]::Round($r.Width, 1); h = [math]::Round($r.Height, 1)
                })
            }
            if ($words.Count -eq 0) { continue }
            $xs = $words | ForEach-Object { $_.x }
            $ys = $words | ForEach-Object { $_.y }
            $x2 = $words | ForEach-Object { $_.x + $_.w }
            $y2 = $words | ForEach-Object { $_.y + $_.h }
            $null = $lines.Add([ordered]@{
                text = $l.Text
                x = [math]::Round(($xs | Measure-Object -Minimum).Minimum, 1)
                y = [math]::Round(($ys | Measure-Object -Minimum).Minimum, 1)
                w = [math]::Round((($x2 | Measure-Object -Maximum).Maximum - ($xs | Measure-Object -Minimum).Minimum), 1)
                h = [math]::Round((($y2 | Measure-Object -Maximum).Maximum - ($ys | Measure-Object -Minimum).Minimum), 1)
                words = $words
            })
        }
        $payload = [ordered]@{
            engine = $engine.RecognizerLanguage.LanguageTag
            width = $bitmap.PixelWidth
            height = $bitmap.PixelHeight
            lines = $lines
        }
        [System.IO.File]::WriteAllText($out, ($payload | ConvertTo-Json -Depth 8 -Compress), $utf8)
        $stream.Dispose()
        [Console]::Out.WriteLine("OK " + $lines.Count)
    } catch {
        [Console]::Out.WriteLine("ERR " + $_.Exception.Message)
    }
    [Console]::Out.Flush()
}
