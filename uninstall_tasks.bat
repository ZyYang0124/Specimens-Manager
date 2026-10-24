@echo off
chcp 65001 >nul
schtasks /delete /tn "SpecimenManagerBackup" /f
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SpecimenManager /f
echo 已取消每日备份计划任务与开机自启。
pause
