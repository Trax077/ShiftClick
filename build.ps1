$ErrorActionPreference = "Stop"

Write-Host "Running tests..." -ForegroundColor Cyan
python -m pytest test_ShiftClick.py -q
if ($LASTEXITCODE -ne 0) {
    Write-Error "Tests failed - aborting build."
    exit 1
}

Write-Host "Building with PyInstaller..." -ForegroundColor Cyan
pyinstaller --noconfirm ShiftClick.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed."
    exit 1
}

Write-Host "Done: dist\ShiftClick.exe" -ForegroundColor Green
