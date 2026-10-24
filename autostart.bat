@echo off
rem Watchdog for the specimen manager server (ASCII only: cmd parses bat files
rem in the ANSI codepage, and UTF-8 Chinese comments break line/label parsing).
rem Loop: (re)start server, wait 5s after it exits, restart again.
cd /d "C:\Research\01_Active_Projects\Table\specimen-manager"
:loop
>> autostart.log echo [%date% %time%] start server process
"C:\Users\l\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" server.py >> autostart.log 2>&1
ping -n 6 127.0.0.1 >nul
goto loop
