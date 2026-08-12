@echo off
cd /d "%~dp0"
git add -A
git commit -m "update"
git push
echo.
echo DONE - Railway is deploying. Wait 2 minutes.
pause
