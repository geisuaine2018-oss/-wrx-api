@echo off
chcp 65001 >nul
title Servidor OEM - Desmonte X (porta 5001)
cd /d "%~dp0"

echo ============================================================
echo   Servidor local de compatibilidade OEM  -  porta 5001
echo ============================================================
echo   Mantenha esta janela ABERTA enquanto usa o painel.
echo   Fechar esta janela DESLIGA o servidor (o preco para de vir).
echo ============================================================
echo.

REM Ja esta rodando? (porta 5001 ocupada)
netstat -ano | findstr ":5001" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo [OK] O servidor JA esta ligado na porta 5001.
  echo      Pode fechar esta janela e usar o painel normalmente.
  echo.
  pause
  exit /b
)

REM Sobe o servidor. Tenta 'python'; se nao houver, tenta 'py'.
where python >nul 2>nul
if %errorlevel%==0 (
  python local_compat_server.py
) else (
  py local_compat_server.py
)

echo.
echo [!] O servidor PAROU. Leia a mensagem acima para ver o motivo.
pause
