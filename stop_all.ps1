# ??????????
$dir = 'C:\Users\li180\Documents\deepseek-harness\default-workspace\weather_watch'
New-Item -ItemType File -Force -Path (Join-Path $dir 'STOP') | Out-Null
foreach ($port in 47821, 47822) {
  $pids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $port } | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($p in $pids) { try { Stop-Process -Id $p -Force -ErrorAction Stop; Write-Output ('stopped pid ' + $p) } catch { Write-Output ('cannot stop pid ' + $p) } }
}
Write-Output 'STOP ??????????????????? STOP ??'