param(
  [string]$Prefix = "",
  [string]$InstallDir = "$HOME\.local\share\chatbridge",
  [string]$Version = "latest",
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Repo = "ylexLiao/chatbridge"
$PythonCommand = $null
$PythonArgs = @()
$PrefixExplicit = -not [string]::IsNullOrWhiteSpace($Prefix)

function Need($Command) {
  if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
    throw "chatbridge install: missing dependency: $Command"
  }
}

function TestPython($Command, [string[]]$Args) {
  if ([string]::IsNullOrWhiteSpace($Command)) {
    return $false
  }
  if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
    return $false
  }
  try {
    & $Command @Args -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" | Out-Null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function UsePython($Command, [string[]]$Args) {
  $script:PythonCommand = (Get-Command $Command).Source
  $script:PythonArgs = $Args
}

function FindPython {
  foreach ($candidate in @($env:CHATBRIDGE_PYTHON, $env:PYTHON, "python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3", "python")) {
    if (TestPython -Command $candidate -Args @()) {
      UsePython -Command $candidate -Args @()
      return
    }
  }

  foreach ($version in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
    if (TestPython -Command "py" -Args @("-$version")) {
      UsePython -Command "py" -Args @("-$version")
      return
    }
  }

  throw "chatbridge install: Python 3.10 or newer is required. Set CHATBRIDGE_PYTHON=C:\Path\To\python.exe or install a newer Python."
}

function PathParts {
  @($env:Path -split [System.IO.Path]::PathSeparator | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function PathHasBin($Bin) {
  foreach ($entry in PathParts) {
    if ([string]::Equals($entry.TrimEnd('\'), $Bin.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function BinIsWritableOrCreatable($Bin) {
  if (Test-Path $Bin) {
    try {
      $probe = Join-Path $Bin (".chatbridge-write-test-" + [System.Guid]::NewGuid().ToString("N"))
      New-Item -ItemType File -Path $probe -Force | Out-Null
      Remove-Item $probe -Force
      return $true
    } catch {
      return $false
    }
  }

  $parent = Split-Path $Bin -Parent
  return (Test-Path $parent) -and (BinIsWritableOrCreatable $parent)
}

function IgnoredPathBin($Bin) {
  $lower = $Bin.ToLowerInvariant()
  return (
    $lower.Contains("\windows\system32") -or
    $lower.Contains("\windows\") -or
    $lower.Contains("\microsoft\windowsapps") -or
    $lower.Contains("\node_modules\.bin") -or
    $lower.Contains("conda") -or
    $lower.Contains("anaconda") -or
    $lower.Contains("miniconda") -or
    $lower.Contains("mambaforge") -or
    $lower.Contains("micromamba")
  )
}

function FindPrefix {
  if ($script:PrefixExplicit) {
    return
  }

  foreach ($bin in @("$HOME\.local\bin", "$HOME\bin")) {
    if ((PathHasBin $bin) -and (BinIsWritableOrCreatable $bin)) {
      $script:Prefix = Split-Path $bin -Parent
      return
    }
  }

  foreach ($bin in PathParts) {
    if (
      ([string]::Equals((Split-Path $bin -Leaf), "bin", [System.StringComparison]::OrdinalIgnoreCase)) -and
      (-not (IgnoredPathBin $bin)) -and
      (BinIsWritableOrCreatable $bin)
    ) {
      $script:Prefix = Split-Path $bin -Parent
      return
    }
  }

  $script:Prefix = "$HOME\.local"
}

function EnsureCurrentPath($Bin) {
  if (-not (PathHasBin $Bin)) {
    $env:Path = "$Bin$([System.IO.Path]::PathSeparator)$env:Path"
    Write-Host "Current PowerShell PATH updated: $Bin"
    Write-Host "Add this directory to PATH for new terminals if needed: $Bin"
  }
}

function QuotePs($Value) {
  "'" + ($Value -replace "'", "''") + "'"
}

function DownloadWithRetry($Uri, $OutFile) {
  $attempts = 3
  for ($attempt = 1; $attempt -le $attempts; $attempt++) {
    try {
      Invoke-WebRequest -Uri $Uri -OutFile $OutFile
      return
    } catch {
      if ($attempt -eq $attempts) {
        throw
      }
      Start-Sleep -Seconds (2 * $attempt)
    }
  }
}

FindPrefix

function SafeRemoveDir($Dir) {
  $resolved = [System.IO.Path]::GetFullPath($Dir)
  $homeResolved = [System.IO.Path]::GetFullPath($HOME)
  $prefixResolved = [System.IO.Path]::GetFullPath($Prefix)
  if (
    [string]::IsNullOrWhiteSpace($resolved) -or
    [string]::Equals($resolved, [System.IO.Path]::GetPathRoot($resolved), [System.StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($resolved, $homeResolved, [System.StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($resolved, $prefixResolved, [System.StringComparison]::OrdinalIgnoreCase)
  ) {
    throw "chatbridge uninstall: refusing to remove unsafe directory: $Dir"
  }
  Remove-Item -Recurse -Force $resolved
}

function UninstallChatBridge {
  $bin = Join-Path $Prefix "bin"
  foreach ($launcher in @((Join-Path $bin "chatbridge.cmd"), (Join-Path $bin "chatbridge.ps1"))) {
    if (Test-Path $launcher) {
      Remove-Item -Force $launcher
      Write-Host "chatbridge uninstall: removed $launcher"
    } else {
      Write-Host "chatbridge uninstall: launcher not found: $launcher"
    }
  }

  if (Test-Path $InstallDir) {
    SafeRemoveDir $InstallDir
    Write-Host "chatbridge uninstall: removed $InstallDir"
  } else {
    Write-Host "chatbridge uninstall: install directory not found: $InstallDir"
  }

  Write-Host "chatbridge uninstall: kept ~/.chatbridge config and source tool histories."
}

if ($Uninstall) {
  UninstallChatBridge
  exit 0
}

FindPython

$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq "AMD64" -or $arch -eq "x86_64") {
  $asset = "chatbridge-windows-x64.zip"
} else {
  throw "chatbridge install: unsupported Windows architecture: $arch"
}

if ($Version -eq "latest") {
  $url = "https://github.com/$Repo/releases/latest/download/$asset"
} else {
  $url = "https://github.com/$Repo/releases/download/$Version/$asset"
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("chatbridge-install-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null

try {
  $archive = Join-Path $tmp $asset
  Write-Host "chatbridge install: downloading $url"
  DownloadWithRetry -Uri $url -OutFile $archive

  Expand-Archive -Path $archive -DestinationPath $tmp -Force
  $payload = Join-Path $tmp "chatbridge"
  if (-not (Test-Path $payload)) {
    throw "chatbridge install: release archive did not contain a chatbridge directory."
  }
  $tui = Join-Path $payload "bin\chatbridge-tui.exe"
  if (-not (Test-Path $tui)) {
    throw "chatbridge install: bundled TUI binary is missing: $tui"
  }
  $oldSmoke = $env:CHATBRIDGE_TUI_SMOKE
  try {
    $env:CHATBRIDGE_TUI_SMOKE = "1"
    & $tui | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "chatbridge install: bundled TUI binary is not runnable on this machine."
    }
  } finally {
    if ($null -eq $oldSmoke) {
      Remove-Item Env:\CHATBRIDGE_TUI_SMOKE -ErrorAction SilentlyContinue
    } else {
      $env:CHATBRIDGE_TUI_SMOKE = $oldSmoke
    }
  }

  if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
  }
  New-Item -ItemType Directory -Path (Split-Path $InstallDir -Parent) -Force | Out-Null
  Move-Item $payload $InstallDir

  $bin = Join-Path $Prefix "bin"
  New-Item -ItemType Directory -Path $bin -Force | Out-Null

  $ps1 = Join-Path $bin "chatbridge.ps1"
  $pythonCommandLiteral = QuotePs $PythonCommand
  $pythonArgsLiteral = (($PythonArgs | ForEach-Object { QuotePs $_ }) -join ", ")
  @(
    "`$bootstrap = @'",
    "import runpy",
    "import sys",
    "sys.path.insert(0, sys.argv.pop(1))",
    "runpy.run_module(`"chatbridge`", run_name=`"__main__`", alter_sys=True)",
    "'@",
    "`$env:PYTHONPATH = $(QuotePs $InstallDir) + [System.IO.Path]::PathSeparator + `$env:PYTHONPATH",
    "`$pythonCommand = $pythonCommandLiteral",
    "`$pythonArgs = @($pythonArgsLiteral)",
    "& `$pythonCommand @pythonArgs -c `$bootstrap $(QuotePs $InstallDir) @args",
    "exit `$LASTEXITCODE"
  ) | Set-Content -Encoding UTF8 $ps1

  $cmd = Join-Path $bin "chatbridge.cmd"
  @"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0chatbridge.ps1" %*
"@ | Set-Content -Encoding ASCII $cmd

  Write-Host "chatbridge installed: $cmd"
  Write-Host "Python: $PythonCommand $($PythonArgs -join ' ')"
  Write-Host "Run: $cmd paths doctor"
  EnsureCurrentPath $bin
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
