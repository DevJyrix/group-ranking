Write-Host "Create a local .env file for this project (will be ignored by git)."
Write-Host "Leave values blank to skip a variable."

$vars = @(
  'ROBLOX_API_KEY', 'ROBLOX_COOKIE', 'GROUP_ID', 'HORNS_RANK_ID',
  'ACCEPT_PENDING', 'RANK_MEMBERS', 'DECLINE_NON_OWNERS', 'DRY_RUN', 'DISCORD_WEBHOOK'
)

$lines = @()
foreach ($name in $vars) {
  $val = Read-Host "$name"
  if ($val -ne '') { $lines += "$name=$val" }
}

if ($lines.Count -eq 0) {
  Write-Host "No values entered; aborting."
  exit 0
}

$content = $lines -join "`n"
$content | Out-File -Encoding utf8 .env
Write-Host ".env written. Make sure .env is not committed (it's in .gitignore)."
Write-Host "Run: git status -- .env"
