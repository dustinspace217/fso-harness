# Register the fso-harness MCP server with Claude Code at USER scope (every project): the
# Windows twin of register.sh. Run setup.ps1 first.
# Remove again with: claude mcp remove --scope user fso-harness
#
#   powershell -ExecutionPolicy Bypass -File mcp\register.ps1 [-WhatIf]
#
# -WhatIf prints the registration command and changes nothing.
#
# The harness's environment is stored in the registration with -e. WHY: Claude Code starts a
# stdio MCP server with its own process environment, not with the shell you registered from.
# A session started from the Start menu has no FS2PATH or FSO_BIN, and every tool would then
# fail with "FS2PATH is not set". Re-run this script after moving the game folder or the
# binaries; the old registration is replaced.
#
# SupportsShouldProcess is what gives the script -WhatIf and -Confirm: the one change it
# makes is wrapped in $PSCmdlet.ShouldProcess below, which returns false under -WhatIf after
# printing what would have been done.
[CmdletBinding(SupportsShouldProcess)]
param()
$ErrorActionPreference = 'Stop'

# A refusal is one plain line on the error stream and an exit code. (Write-Error would wrap
# the line in PowerShell's own error report, three more lines that say nothing.)
function Exit-WithMessage([string]$Message, [int]$Code) {
	[Console]::Error.WriteLine($Message)
	exit $Code
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
	Exit-WithMessage "run $(Join-Path $PSScriptRoot 'setup.ps1') first" 1
}

# The two values every tool needs. [Environment]::GetEnvironmentVariable reads a variable by
# name and returns nothing when it is not set.
$missing = @('FS2PATH', 'FSO_BIN') | Where-Object { -not [Environment]::GetEnvironmentVariable($_) }
if ($missing) {
	Exit-WithMessage "not registered: set $($missing -join ' and ') first (the registration stores them)" 2
}

# One "-e NAME=value" pair per variable that is set. The last four are optional.
# FSO_HARNESS_RUNTIME is among them because `launch` refuses a run folder whose path has a
# letter outside ASCII and tells the user to set it: set in a shell only, it would never
# reach a server that Claude Code starts.
$stored = @()
foreach ($name in 'FS2PATH', 'FSO_BIN', 'FSO_BIN_DEBUG', 'QTFRED_BIN', 'FSO_HARNESS_MAX_SESSIONS',
		'FSO_HARNESS_RUNTIME') {
	$value = [Environment]::GetEnvironmentVariable($name)
	if ($value) {
		$stored += @('-e', "$name=$value")
	}
}

# The name goes BEFORE the -e list: -e takes any number of values, so a name placed after it
# is read as one more NAME=value and rejected ("Invalid environment variable format").
$command = @('mcp', 'add', '--scope', 'user', 'fso-harness') + $stored +
	@('--', $python, (Join-Path $PSScriptRoot 'server.py'))

if ($PSCmdlet.ShouldProcess('the Claude Code user configuration', "claude $($command -join ' ')")) {
	if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
		Exit-WithMessage 'the claude command is not on PATH; install Claude Code first' 3
	}
	# Replace an existing registration, so a re-run with new paths does not fail on "already
	# exists". Its output and its failure (nothing to remove) are both of no interest. In a
	# try: with $ErrorActionPreference 'Stop', Windows PowerShell 5.1 is said to turn a
	# program's redirected error output into an error that ends the script, and on a first
	# run there is nothing to remove (QA Plan B, CR-16; not run: this path needs Claude Code).
	try { & claude mcp remove --scope user fso-harness *> $null } catch { }
	& claude @command
	if ($LASTEXITCODE -ne 0) {
		throw "claude mcp add failed (exit code $LASTEXITCODE)"
	}
	Write-Output "registered with $(($stored | Where-Object { $_ -ne '-e' }) -join ' ')"
	Write-Output 'Restart Claude Code to pick it up; check with: claude mcp list'
}
