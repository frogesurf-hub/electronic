param([string]$ImagePath, [string]$Language = 'zh-Hans-CN', [switch]$ListLanguages)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]

if ($ListLanguages) {
    @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages.LanguageTag) | ConvertTo-Json
    exit 0
}

function Await-WinRT($Operation, [Type]$ResultType) {
    $Bridge = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $Task = $Bridge.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $Task.GetAwaiter().GetResult()
}

$Engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
    (New-Object Windows.Globalization.Language($Language)))
if ($null -eq $Engine) { throw 'OCR_LANGUAGE_UNAVAILABLE' }
$File = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync(
    (Resolve-Path -LiteralPath $ImagePath).Path)) ([Windows.Storage.StorageFile])
$Stream = Await-WinRT ($File.OpenAsync([Windows.Storage.FileAccessMode]::Read)) (
    [Windows.Storage.Streams.IRandomAccessStream])
$Bitmap = $null
try {
    $Decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($Stream)) (
        [Windows.Graphics.Imaging.BitmapDecoder])
    $Bitmap = Await-WinRT ($Decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $Result = Await-WinRT ($Engine.RecognizeAsync($Bitmap)) ([Windows.Media.Ocr.OcrResult])
    $Lines = @($Result.Lines | ForEach-Object {
        $Line = $_
        $Words = @($Line.Words | ForEach-Object {
            @{text=$_.Text; bbox=@($_.BoundingRect.X, $_.BoundingRect.Y,
                ($_.BoundingRect.X + $_.BoundingRect.Width), ($_.BoundingRect.Y + $_.BoundingRect.Height))}
        })
        @{text=$Line.Text; words=$Words}
    })
    @{language=$Engine.RecognizerLanguage.LanguageTag; width=$Bitmap.PixelWidth;
        height=$Bitmap.PixelHeight; lines=$Lines} | ConvertTo-Json -Depth 10
} finally {
    if ($null -ne $Bitmap) { $Bitmap.Dispose() }
    $Stream.Dispose()
}
