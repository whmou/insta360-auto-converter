param(
    [string]$Source,
    [string]$PlaylistId,
    [string]$Config,
    [int]$MaxCount,
    [switch]$AdoptExisting,
    [switch]$KeepUploaded
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Config) {
    $externalConfig = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Insta360AutoConverter\config.psd1"
    $legacyConfig = Join-Path $PSScriptRoot ".local\config.psd1"
    $Config = if (Test-Path -LiteralPath $externalConfig -PathType Leaf) {
        $externalConfig
    } else {
        $legacyConfig
    }
}

if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Local config not found: $Config. Copy config.example.psd1 to %LOCALAPPDATA%\Insta360AutoConverter\config.psd1 and edit it. Existing .local\config.psd1 files remain supported."
}

$settings = Import-PowerShellDataFile -LiteralPath $Config
foreach ($name in @("DataDirectory", "SdkExecutable", "ExpectedChannelId")) {
    if (-not $settings[$name]) {
        throw "Missing required setting '$name' in $Config"
    }
}

$data = [Environment]::ExpandEnvironmentVariables([string]$settings.DataDirectory)
$sdk = [Environment]::ExpandEnvironmentVariables([string]$settings.SdkExecutable)
$token = if ($settings.TokenFile) {
    [Environment]::ExpandEnvironmentVariables([string]$settings.TokenFile)
} else {
    Join-Path $data "youtube_auth.json"
}
$output = if ($settings.OutputDirectory) {
    [Environment]::ExpandEnvironmentVariables([string]$settings.OutputDirectory)
} else {
    Join-Path $data "sd-output"
}
$dailyLimit = if ($settings.DailyLimit) { [int]$settings.DailyLimit } else { 80 }
$privacy = if ($settings.Privacy) { [string]$settings.Privacy } else { "unlisted" }
$targetPlaylist = if ($PlaylistId) { $PlaylistId } else { [string]$settings.DefaultPlaylistId }

$pipelineArgs = @(
    "-m", "insta360_converter",
    "run-sd", $output,
    "--sdk-exe", $sdk,
    "--token", $token,
    "--expected-channel-id", [string]$settings.ExpectedChannelId,
    "--daily-limit", $dailyLimit,
    "--privacy", $privacy
)

if ($Source) {
    $pipelineArgs += @("--source", $Source)
}
if ($targetPlaylist) {
    $pipelineArgs += @("--playlist-id", $targetPlaylist)
}
if ($settings.LoginHint) {
    $pipelineArgs += @("--login-hint", [string]$settings.LoginHint)
}
if ($settings.ClientSecretFile) {
    $clientSecret = [Environment]::ExpandEnvironmentVariables([string]$settings.ClientSecretFile)
    if (Test-Path -LiteralPath $clientSecret -PathType Leaf) {
        $pipelineArgs += @("--client-secret", $clientSecret)
    }
}
if ($KeepUploaded) {
    $pipelineArgs += "--keep-uploaded"
}
if ($AdoptExisting) {
    $pipelineArgs += "--adopt-existing"
}
if ($MaxCount -gt 0) {
    $pipelineArgs += @("--max-count", $MaxCount)
}

Push-Location $repo
try {
    & python @pipelineArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Insta360 SD pipeline stopped with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
