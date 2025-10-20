@echo off
chcp 65001 >nul
title Bitget Auto Server (pm2)
cd /d D:\bitget\bg_bot

echo ===============================
echo  Bitget Auto Server Start
echo ===============================

:: 서버 실행
pm2 start server.js --name bg_server
pm2 start trade.js --name bg_trade

:: 상태 확인
pm2 list

echo ===============================
echo  Bitget Servers are running.
echo  Keep this window open.
echo ===============================

pause
