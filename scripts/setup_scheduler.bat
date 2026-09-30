@echo off
chcp 65001 >nul
echo ================================================
echo  四六级真题自动更新 - 计划任务安装
echo ================================================
echo.

set PYTHON_PATH=C:\Users\陈富林\PycharmProjects\cet4\cet4\cet4--6\venv\Scripts\python.exe
set WORK_DIR=C:\Users\陈富林\PycharmProjects\cet4\cet4\cet4--6\cet-agent

echo [1/2] 删除已存在的任务（如有）...
schtasks /Delete /TN "CET-Paper-Auto-Update" /F >nul 2>&1

echo [2/2] 创建新的计划任务...
schtasks /Create ^
    /TN "CET-Paper-Auto-Update" ^
    /TR "\"%PYTHON_PATH%\" \"%WORK_DIR%\scripts\auto_update_papers.py\"" ^
    /SC WEEKLY ^
    /D MON ^
    /ST 02:00 ^
    /RL HIGHEST ^
    /F

if %errorlevel% equ 0 (
    echo.
    echo ✅ 计划任务创建成功！
    echo    执行时间：每周一 02:00
    echo    任务名称：CET-Paper-Auto-Update
    echo.
    echo 查看任务：taskschd.msc
    echo 手动运行：schtasks /Run /TN "CET-Paper-Auto-Update"
) else (
    echo.
    echo ❌ 创建失败，请确认以管理员身份运行此脚本
)

echo.
pause