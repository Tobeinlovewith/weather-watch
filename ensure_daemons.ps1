param([switch]$Verbose)
# ????????????????????????????? STOP/??/???
$pyw = 'C:\Users\li180\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe'
$dir = 'C:\Users\li180\Documents\deepseek-harness\default-workspace\weather_watch'
$log = Join-Path $dir 'logs\ensure.log'
$stopFlag = Join-Path $dir 'STOP'

function Log($m) { Add-Content -Path $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $m) -Encoding UTF8 }

if (Test-Path $stopFlag) { Log 'STOP flag present -> skip'; exit 0 }

$up = $false
try {
  $c = New-Object System.Net.Sockets.TcpClient
  $c.Connect('127.0.0.1', 47820)
  $up = $true
  $c.Close()
} catch { $up = $false }
if ($up) { if ($Verbose) { Write-Output 'watchdog: running' }; exit 0 }

$n = @(Get-Process pythonw -ErrorAction SilentlyContinue).Count
if ($n -ge 4) { Log ('pythonw=' + $n + ' >= 4 -> refuse'); exit 0 }

$body = '"' + $pyw + '" "' + $dir + '\watchdog.pyw"'
$null = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine      = $body
  CurrentDirectory = $dir
}
Log 'start watchdog'
Write-Output 'watchdog: started'
exit 0