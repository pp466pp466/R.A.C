# Container acceptance wrapper (ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less scripts with the
# system code page, so non-ASCII text here would be garbled). The real logic is in verify_container.py.
#
# Usage:  .\scripts\verify-container.ps1            # build + verify
#         .\scripts\verify-container.ps1 --no-build # verify an existing image
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'verify_container.py') @args
exit $LASTEXITCODE
