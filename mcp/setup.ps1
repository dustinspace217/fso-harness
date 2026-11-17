# Create the MCP wrapper's isolated venv (the Windows twin of setup.sh).
# fsoctl itself is stdlib-only; only this optional layer needs the `mcp` package, so it lives
# in mcp\.venv and nowhere else.
# Registering the server with Claude Code is a separate step (register.ps1): it changes your
# user-level Claude Code configuration, so it is never done as a side effect of setup.
#
#   powershell -ExecutionPolicy Bypass -File mcp\setup.ps1 [-Python C:\path\to\python.exe]
#
# -Python: the Python 3.12+ to build the venv from. The default is whatever `python` is on
# PATH; name one when that is the Microsoft Store placeholder or an older version.
[CmdletBinding()]
param(
	[string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'

# $PSScriptRoot is the folder this script is in (PowerShell sets it for a running script).
$venv = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
	Write-Output "creating venv at $venv"
	& $Python -m venv $venv
	# A program's failure does not stop a PowerShell script by itself: its exit code has to be
	# read. $LASTEXITCODE holds the exit code of the last program run.
	if ($LASTEXITCODE -ne 0) {
		throw "could not create the venv with '$Python' (exit code $LASTEXITCODE); pass -Python <path to python.exe>"
	}
}

# mcp 2.0.0: the version this server was written and tested against (MCPServer, Image).
& $venvPython -m pip install --quiet 'mcp==2.0.0'
if ($LASTEXITCODE -ne 0) {
	throw "pip could not install mcp==2.0.0 (exit code $LASTEXITCODE)"
}
Write-Output "venv ready. To make the tools available in Claude Code, run: $(Join-Path $PSScriptRoot 'register.ps1')"
