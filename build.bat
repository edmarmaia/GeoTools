@echo off
REM Script de build local para GeoTools.exe
REM Requer: pip install pyinstaller

echo Instalando/atualizando PyInstaller...
pip install --quiet --upgrade pyinstaller

echo.
echo Gerando GeoTools.exe...
pyinstaller GeoTools.spec --clean --noconfirm

echo.
if exist "dist\GeoTools.exe" (
    echo BUILD OK: dist\GeoTools.exe
) else (
    echo ERRO: executavel nao gerado. Verifique os logs acima.
    exit /b 1
)
