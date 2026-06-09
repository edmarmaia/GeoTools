# GeoTools — Instalador
# Uso:
#   irm https://raw.githubusercontent.com/edmarmaia/GeoTools/main/install.ps1 | iex

$ErrorActionPreference = 'Stop'

$REPO        = "edmarmaia/GeoTools"
$EXE_NAME    = "GeoTools.exe"
$INSTALL_DIR = Join-Path $env:LOCALAPPDATA "GeoTools"
$DEST        = Join-Path $INSTALL_DIR $EXE_NAME

Write-Host ""
Write-Host "  GeoTools — Instalador" -ForegroundColor Cyan
Write-Host "  =====================" -ForegroundColor Cyan
Write-Host ""

# --- Busca a ultima versao no GitHub ---
Write-Host "  Buscando ultima versao..." -NoNewline
try {
    $release = Invoke-RestMethod "https://api.github.com/repos/$REPO/releases/latest"
} catch {
    Write-Host ""
    Write-Error "Nao foi possivel acessar o GitHub. Verifique sua conexao com a internet."
    exit 1
}

$version = $release.tag_name
$asset   = $release.assets | Where-Object { $_.name -eq $EXE_NAME } | Select-Object -First 1

if (-not $asset) {
    Write-Host ""
    Write-Error "Nenhum executavel encontrado na release '$version'. Aguarde o build ser publicado."
    exit 1
}

$sizeMB = [math]::Round($asset.size / 1MB, 1)
Write-Host " $version  ($sizeMB MB)" -ForegroundColor Green

# --- Verifica se ja esta instalado ---
if (Test-Path $DEST) {
    Write-Host "  Versao anterior encontrada em: $DEST"
    Write-Host "  Atualizando..." -ForegroundColor Yellow
}

# --- Cria diretorio de instalacao ---
if (-not (Test-Path $INSTALL_DIR)) {
    New-Item -ItemType Directory -Path $INSTALL_DIR | Out-Null
}

# --- Download ---
Write-Host "  Baixando $EXE_NAME..."
try {
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $DEST -UseBasicParsing
} catch {
    Write-Error "Falha no download: $_"
    exit 1
}

Write-Host "  Download concluido." -ForegroundColor Green

# --- Adiciona ao PATH do usuario (permanente) ---
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User") ?? ""
if ($userPath -notlike "*$INSTALL_DIR*") {
    $newPath = ($userPath.TrimEnd(';') + ";$INSTALL_DIR").TrimStart(';')
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    # Aplica na sessao atual tambem
    $env:PATH = $env:PATH.TrimEnd(';') + ";$INSTALL_DIR"
    Write-Host "  PATH atualizado: $INSTALL_DIR" -ForegroundColor Yellow
}

# --- Resultado ---
Write-Host ""
Write-Host "  Instalacao concluida!" -ForegroundColor Green
Write-Host "  Versao : $version"
Write-Host "  Local  : $DEST"
Write-Host ""
Write-Host "  Como usar:" -ForegroundColor Cyan
Write-Host "    GeoTools"
Write-Host "    GeoTools `"arquivo.gpx`""
Write-Host ""
Write-Host "  Se o comando nao for reconhecido, abra um novo terminal." -ForegroundColor Yellow
Write-Host ""
