#Requires -Version 5.1
# Download and install ExternalGameSync from GitHub.
# No admin rights required.
#
# HOW TO RUN — paste this into PowerShell or Command Prompt:
#
#   PowerShell:
#     iex (iwr 'https://raw.githubusercontent.com/pmahern/steamexternalgamesync/master/download-install.ps1' -UseBasicParsing).Content
#
#   Command Prompt:
#     powershell -c "iex (iwr 'https://raw.githubusercontent.com/pmahern/steamexternalgamesync/master/download-install.ps1' -UseBasicParsing).Content"
#
# Running via iex/iwr executes the script in memory — no file is written to disk,
# so Windows SmartScreen and execution-policy restrictions do not apply.
# The downloaded archive is extracted by .NET (not Expand-Archive), which does not
# attach a Zone.Identifier to extracted files, so install.ps1 runs unblocked.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor
    [Net.SecurityProtocolType]::Tls13 -bor
    [Net.ServicePointManager]::SecurityProtocol

$API_URL = 'https://api.github.com/repos/pmahern/steamexternalgamesync/releases/latest'

Write-Host ''
Write-Host '  ExternalGameSync - Download & Install'
Write-Host '  ======================================'
Write-Host ''

# ── Python check ──────────────────────────────────────────────────────────────
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host '[error] Python 3 is required but not found.'
    Write-Host '        Download from https://www.python.org/downloads/'
    Write-Host "        Check 'Add Python to PATH' during install, then re-run this command."
    if ($Host.Name -eq 'ConsoleHost') { Read-Host 'Press Enter to exit' }
    exit 1
}
Write-Host "[ok] $(python --version 2>&1)"

# ── Fetch latest release info ──────────────────────────────────────────────────
$tmp     = Join-Path $env:TEMP "egs_install_$(Get-Random)"
$zipFile = Join-Path $tmp 'release.zip'

try {
    New-Item -ItemType Directory -Path $tmp | Out-Null

    Write-Host 'Fetching latest release info from GitHub...'
    # WebClient.DownloadString does NOT set a Zone.Identifier (Mark of the Web),
    # unlike Invoke-WebRequest on modern Windows — same mechanism used by the
    # built-in 'externalgamesync update' command.
    $wc      = New-Object Net.WebClient
    $release = ConvertFrom-Json $wc.DownloadString($API_URL)
    $zipUrl  = $release.zipball_url
    $tag     = if ($release.tag_name) { $release.tag_name } else { 'latest' }
    Write-Host "[ok] Latest release: $tag"

    Write-Host "Downloading $tag..."
    $wc.DownloadFile($zipUrl, $zipFile)
    Write-Host '[ok] Downloaded'

    Write-Host 'Extracting...'
    # Use .NET ZipFile directly — avoids the MOTW propagation that Expand-Archive
    # performs when the source zip itself carries a Zone.Identifier.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zipFile, $tmp)

    $extracted = Get-ChildItem -Path $tmp -Directory |
                 Where-Object { $_.Name -notlike '*.zip' } |
                 Select-Object -First 1
    if (-not $extracted) {
        Write-Host '[error] Could not find extracted directory - archive may be corrupt.'
        exit 1
    }
    Write-Host "[ok] Extracted to $($extracted.FullName)"

    Write-Host ''
    Write-Host 'Running installer...'
    Write-Host ''
    & powershell -ExecutionPolicy Bypass -File (Join-Path $extracted.FullName 'install.ps1')
    exit $LASTEXITCODE
} catch {
    Write-Host "[error] Installation failed: $_"
    exit 1
} finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
