# 启动全部：虚拟机 MySQL -> 本机 Redis -> hmdp(8081) -> ai-service(8000)
# 用法： .\scripts\start-all.ps1
#        想跳过虚拟机检测： .\scripts\start-all.ps1 -SkipVm
& (Join-Path $PSScriptRoot 'dev.ps1') -Action start @args