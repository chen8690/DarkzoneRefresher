param(
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Lang = "zh-Hans-CN"
)

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

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path $Image).Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$language = New-Object Windows.Globalization.Language $Lang
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "no OCR engine available" }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
    $words = New-Object System.Collections.ArrayList
    foreach ($w in $line.Words) {
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
        text = $line.Text
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
$json = $payload | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("OK lines=" + $lines.Count + " -> " + $Out)
