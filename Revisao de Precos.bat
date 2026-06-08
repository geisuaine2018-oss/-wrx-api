@echo off
chcp 65001 >nul
title Revisao de Precos - Dominio das Pecas
cd /d "%~dp0"
echo.
echo ============================================
echo   REVISAO DE PRECOS - raspagem no seu PC
echo ============================================
echo.
set /p QTD=Quantos anuncios revisar? (numero, ou "tudo") [Enter = 10]:
if "%QTD%"=="" set QTD=10
echo.
echo Rodando para %QTD% anuncios... (pode demorar alguns minutos)
echo.
python revisao_precos_local.py %QTD%
echo.
echo ============================================
echo  Pronto! Abra a tela REVISAO DE PRECOS no
echo  painel para aprovar / editar / ignorar.
echo ============================================
echo.
pause
