# 停止全部服务（虚拟机不会被关闭）
# 用法： .\scripts\stop-all.ps1
& (Join-Path $PSScriptRoot 'dev.ps1') -Action stop @args