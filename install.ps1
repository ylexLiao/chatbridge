param(
  [string]$Prefix = $(if (-not [string]::IsNullOrWhiteSpace($env:CHATBRIDGE_PREFIX)) { $env:CHATBRIDGE_PREFIX } else { "" }),
  [string]$InstallDir = $(if (-not [string]::IsNullOrWhiteSpace($env:CHATBRIDGE_INSTALL_DIR)) { $env:CHATBRIDGE_INSTALL_DIR } else { "$HOME\.local\share\chatbridge" }),
  [string]$Version = $(if (-not [string]::IsNullOrWhiteSpace($env:CHATBRIDGE_VERSION)) { $env:CHATBRIDGE_VERSION } else { "latest" }),
  [switch]$ForceReinstall,
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Repo = "ylexLiao/chatbridge"
$ReleaseBase = $env:CHATBRIDGE_RELEASE_BASE
$PythonCommand = $null
$PythonArgs = @()
$PrefixExplicit = -not [string]::IsNullOrWhiteSpace($Prefix)

function Need($Command) {
  if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
    throw "chatbridge install: missing dependency: $Command"
  }
}

function ProbePython($Command, [string[]]$Args) {
  # Returns "ok" (>=3.10 system interpreter), "env" (>=3.10 but venv/conda), or "bad".
  if ([string]::IsNullOrWhiteSpace($Command)) {
    return "bad"
  }
  if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
    return "bad"
  }
  try {
    & $Command @Args -c "import sys; sys.exit(2 if sys.version_info < (3, 10) else (3 if sys.prefix != getattr(sys, 'base_prefix', sys.prefix) else 0))" | Out-Null
    switch ($LASTEXITCODE) {
      0 { return "ok" }
      3 { return "env" }
      default { return "bad" }
    }
  } catch {
    return "bad"
  }
}

function PythonPathIsConda($Command) {
  $source = (Get-Command $Command -ErrorAction SilentlyContinue).Source
  if ([string]::IsNullOrWhiteSpace($source)) {
    return $false
  }
  $lower = $source.ToLowerInvariant()
  return (
    $lower.Contains("conda") -or
    $lower.Contains("anaconda") -or
    $lower.Contains("miniconda") -or
    $lower.Contains("mambaforge") -or
    $lower.Contains("micromamba") -or
    $lower.Contains("\envs\")
  )
}

function UsePython($Command, [string[]]$Args) {
  $script:PythonCommand = (Get-Command $Command).Source
  $script:PythonArgs = $Args
}

function FindPython {
  foreach ($candidate in @($env:CHATBRIDGE_PYTHON, $env:PYTHON)) {
    if ([string]::IsNullOrWhiteSpace($candidate)) {
      continue
    }
    if ((ProbePython -Command $candidate -Args @()) -ne "bad") {
      UsePython -Command $candidate -Args @()
      return
    }
    throw "chatbridge install: CHATBRIDGE_PYTHON/PYTHON points at $candidate, which is missing or older than Python 3.10."
  }

  $envFallback = $null
  $envFallbackArgs = @()
  foreach ($candidate in @("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3", "python")) {
    $probe = ProbePython -Command $candidate -Args @()
    if ($probe -eq "bad") {
      continue
    }
    if ($probe -eq "ok" -and -not (PythonPathIsConda $candidate)) {
      UsePython -Command $candidate -Args @()
      return
    }
    if ($null -eq $envFallback) {
      $envFallback = $candidate
      $envFallbackArgs = @()
    }
  }

  foreach ($version in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
    if ((ProbePython -Command "py" -Args @("-$version")) -eq "ok") {
      UsePython -Command "py" -Args @("-$version")
      return
    }
  }

  if ($null -ne $envFallback) {
    UsePython -Command $envFallback -Args $envFallbackArgs
    Write-Warning "chatbridge install: only a conda/virtualenv Python was found: $script:PythonCommand"
    Write-Warning "chatbridge install: the chatbridge launcher will pin this interpreter; set CHATBRIDGE_PYTHON to use another one."
    return
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

function DirIsWritable($Dir) {
  try {
    $probe = Join-Path $Dir (".chatbridge-write-test-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType File -Path $probe -Force | Out-Null
    Remove-Item $probe -Force
    return $true
  } catch {
    return $false
  }
}

function BinIsWritableOrCreatable($Bin) {
  if (Test-Path $Bin -PathType Container) {
    return DirIsWritable $Bin
  }
  if (Test-Path $Bin) {
    return $false
  }

  # Mirror install.sh: only allow creating the bin dir when its immediate parent
  # already exists and is writable.
  $parent = Split-Path $Bin -Parent
  if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path $parent -PathType Container)) {
    return $false
  }
  return DirIsWritable $parent
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
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl) {
    & $curl.Source --http1.1 -fL --retry 3 --retry-delay 2 --retry-max-time 120 $Uri -o $OutFile
    if ($LASTEXITCODE -eq 0) {
      return
    }
    Write-Warning "chatbridge install: curl.exe download failed (exit $LASTEXITCODE); retrying with Invoke-WebRequest."
  }
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
  $prefixWithSep = $prefixResolved.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
  $resolvedWithSep = $resolved.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
  if (
    [string]::IsNullOrWhiteSpace($resolved) -or
    [string]::Equals($resolved, [System.IO.Path]::GetPathRoot($resolved), [System.StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($resolved, $homeResolved, [System.StringComparison]::OrdinalIgnoreCase) -or
    [string]::Equals($resolved, $prefixResolved, [System.StringComparison]::OrdinalIgnoreCase) -or
    $prefixWithSep.StartsWith($resolvedWithSep, [System.StringComparison]::OrdinalIgnoreCase)
  ) {
    throw "chatbridge uninstall: refusing to remove unsafe directory: $Dir"
  }
  Remove-Item -Recurse -Force $resolved
}

function LooksLikeChatBridgeInstall($Dir) {
  return (
    (Test-Path (Join-Path $Dir "chatbridge\__init__.py")) -or
    (Test-Path (Join-Path $Dir "bin\chatbridge-tui.exe")) -or
    (Test-Path (Join-Path $Dir "bin\chatbridge-tui"))
  )
}

function RemoveInstallDir($Label) {
  if (-not (Test-Path $InstallDir)) {
    return
  }
  if (-not $ForceReinstall -and -not (LooksLikeChatBridgeInstall $InstallDir)) {
    throw ("chatbridge ${Label}: refusing to remove ${InstallDir}: it does not look like a ChatBridge install " +
      "(expected chatbridge\__init__.py or bin\chatbridge-tui.exe inside). Re-run with -ForceReinstall to remove it anyway.")
  }
  SafeRemoveDir $InstallDir
}

function UninstallChatBridge {
  $binDirs = New-Object System.Collections.Generic.List[string]
  # Only consult PATH when no explicit -Prefix was given, so an uninstall scoped
  # to a test prefix can never delete an unrelated global install.
  if (-not $script:PrefixExplicit) {
    $existing = Get-Command chatbridge -ErrorAction SilentlyContinue
    if ($existing -and $existing.Source) {
      $binDirs.Add((Split-Path $existing.Source -Parent))
    }
  }
  $binDirs.Add((Join-Path $Prefix "bin"))

  $seen = @{}
  $removedAny = $false
  foreach ($binDir in $binDirs) {
    $key = $binDir.ToLowerInvariant().TrimEnd('\')
    if ($seen.ContainsKey($key)) {
      continue
    }
    $seen[$key] = $true
    foreach ($name in @("chatbridge.cmd", "chatbridge.ps1", "chatbridge-tui.exe", "chatbridge-tui")) {
      $launcher = Join-Path $binDir $name
      if (Test-Path $launcher) {
        Remove-Item -Force $launcher
        Write-Host "chatbridge uninstall: removed $launcher"
        $removedAny = $true
      }
    }
  }
  if (-not $removedAny) {
    Write-Host ("chatbridge uninstall: launcher not found: " + (Join-Path (Join-Path $Prefix "bin") "chatbridge.cmd"))
  }

  if (Test-Path $InstallDir) {
    RemoveInstallDir "uninstall"
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

if (-not [string]::IsNullOrWhiteSpace($ReleaseBase)) {
  if ($Version -ne "latest") {
    Write-Warning "chatbridge install: CHATBRIDGE_RELEASE_BASE is set; it overrides version $Version (assets come from $($ReleaseBase.TrimEnd('/')))."
  }
  $url = "$($ReleaseBase.TrimEnd('/'))/$asset"
} elseif ($Version -eq "latest") {
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
    # Relax EAP: under "Stop", 2>&1 on a native command can throw on the first
    # stderr line in Windows PowerShell 5.1 before we can show the output.
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $smokeOutput = & $tui 2>&1
    $ErrorActionPreference = $oldEap
    if ($LASTEXITCODE -ne 0) {
      if ($smokeOutput) {
        Write-Host (($smokeOutput | ForEach-Object { "$_" }) -join [System.Environment]::NewLine)
      }
      throw "chatbridge install: bundled TUI binary is not runnable on this machine."
    }
  } finally {
    if ($null -eq $oldSmoke) {
      Remove-Item Env:\CHATBRIDGE_TUI_SMOKE -ErrorAction SilentlyContinue
    } else {
      $env:CHATBRIDGE_TUI_SMOKE = $oldSmoke
    }
  }

  RemoveInstallDir "install"
  New-Item -ItemType Directory -Path (Split-Path $InstallDir -Parent) -Force | Out-Null
  Move-Item $payload $InstallDir

  $bin = Join-Path $Prefix "bin"
  New-Item -ItemType Directory -Path $bin -Force | Out-Null

  $ps1 = Join-Path $bin "chatbridge.ps1"
  $pythonCommandLiteral = QuotePs $PythonCommand
  $pythonArgsLiteral = (($PythonArgs | ForEach-Object { QuotePs $_ }) -join ", ")
  $prefixLiteral = QuotePs $Prefix
  $installDirLiteral = QuotePs $InstallDir
  @(
    "`$bootstrap = @'",
    "import runpy",
    "import sys",
    "sys.path.insert(0, sys.argv.pop(1))",
    "runpy.run_module(`"chatbridge`", run_name=`"__main__`", alter_sys=True)",
    "'@",
    "if ([string]::IsNullOrWhiteSpace(`$env:CHATBRIDGE_PREFIX)) { `$env:CHATBRIDGE_PREFIX = $prefixLiteral }",
    "if ([string]::IsNullOrWhiteSpace(`$env:CHATBRIDGE_INSTALL_DIR)) { `$env:CHATBRIDGE_INSTALL_DIR = $installDirLiteral }",
    "if ([string]::IsNullOrWhiteSpace(`$env:CHATBRIDGE_INSTALLER_URL)) { `$env:CHATBRIDGE_INSTALLER_URL = 'https://github.com/ylexLiao/chatbridge/releases/latest/download/install.ps1' }",
    "if ([string]::IsNullOrEmpty(`$env:PYTHONPATH)) {",
    "  `$env:PYTHONPATH = `$env:CHATBRIDGE_INSTALL_DIR",
    "} else {",
    "  `$env:PYTHONPATH = `$env:CHATBRIDGE_INSTALL_DIR + [System.IO.Path]::PathSeparator + `$env:PYTHONPATH",
    "}",
    "`$pythonCommand = $pythonCommandLiteral",
    "`$pythonArgs = @($pythonArgsLiteral)",
    "& `$pythonCommand @pythonArgs -c `$bootstrap `$env:CHATBRIDGE_INSTALL_DIR @args",
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
