param(
  [Parameter(Mandatory=$true)][string]$Path,
  [string]$OutJson = "",
  [double]$Scale = 2.0,
  [string]$LanguageTag = "en-US"
)

# Windows built-in OCR (no external install). Renders the screenshot at $Scale before
# recognition: the ODIN grid font is ~9px and drops single characters at 1:1.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

[Windows.Media.Ocr.OcrEngine,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                 $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($winRtTask, $resultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
  $netTask = $asTask.Invoke($null, @($winRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}

$original = $Path
$cleanup = $null
if ($Scale -ne 1.0) {
  $src = [System.Drawing.Image]::FromFile($Path)
  $dst = New-Object System.Drawing.Bitmap ([int]($src.Width * $Scale)), ([int]($src.Height * $Scale))
  $g = [System.Drawing.Graphics]::FromImage($dst)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  $g.DrawImage($src, 0, 0, $dst.Width, $dst.Height)
  $g.Dispose(); $src.Dispose()
  $cleanup = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(),
              ("odin_ocr_{0}.png" -f [Guid]::NewGuid().ToString("N")))
  $dst.Save($cleanup, [System.Drawing.Imaging.ImageFormat]::Png)
  $dst.Dispose()
  $Path = $cleanup
}

$imgW = 0; $imgH = 0
try {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $imgW = $decoder.PixelWidth; $imgH = $decoder.PixelHeight
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

  $engine = $null
  $langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
  $pick = $langs | Where-Object { $_.LanguageTag -eq $LanguageTag } | Select-Object -First 1
  if ($null -eq $pick -and $langs.Count -gt 0) { $pick = $langs[0] }
  if ($null -ne $pick) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($pick) }
  if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::FromUserProfileLanguages() }
  if ($null -eq $engine) { throw "No OCR recognizer language installed. Add English via Settings > Time & Language > Language." }

  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

  $lines = New-Object System.Collections.Generic.List[object]
  foreach ($ln in $result.Lines) {
    $words = New-Object System.Collections.Generic.List[object]
    foreach ($w in $ln.Words) {
      $r = $w.BoundingRect
      $words.Add([pscustomobject]@{
        t = $w.Text; x = [math]::Round([double]$r.X, 2); y = [math]::Round([double]$r.Y, 2)
        w = [math]::Round([double]$r.Width, 2); h = [math]::Round([double]$r.Height, 2)
      })
    }
    $lr = $ln.BoundingRect
    $lines.Add([pscustomobject]@{
      y = [math]::Round([double]$lr.Y, 2); text = $ln.Text; words = $words
    })
  }

  $payload = [pscustomobject]@{
    width = $imgW; height = $imgH; scale = $Scale
    language = $engine.RecognizerLanguage.LanguageTag; lines = $lines
  }
  if ($OutJson -eq "") { $payload | ConvertTo-Json -Depth 6 -Compress }
  else { $payload | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $OutJson }
  Write-Output ("OCR_OK lines=" + $lines.Count + " size=" + $imgW + "x" + $imgH)
}
finally {
  if ($cleanup -and (Test-Path $cleanup)) { Remove-Item $cleanup -Force -ErrorAction SilentlyContinue }
}
