Write-Host "Set GitHub Actions repository secrets using gh CLI."

$required = @('ROBLOX_API_KEY','ROBLOX_COOKIE','GROUP_ID','HORNS_RANK_ID')
$optional = @('ACCEPT_PENDING','RANK_MEMBERS','DECLINE_NON_OWNERS','DRY_RUN','DISCORD_WEBHOOK')

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
  Write-Host "gh CLI not found. Install GitHub CLI and authenticate (gh auth login)." -ForegroundColor Yellow
  exit 1
}

function Set-Secret($name) {
  $val = Read-Host -Prompt "Enter value for $name (leave blank to skip)"
  if ($val -ne '') {
    gh secret set $name --body $val
    Write-Host "Set secret: $name"
  } else {
    Write-Host "Skipped: $name"
  }
}

Write-Host "Adding required secrets..."
foreach ($s in $required) { Set-Secret $s }

Write-Host "Optional secrets (press Enter to skip)..."
foreach ($s in $optional) { Set-Secret $s }

Write-Host "All done. Verify secrets at: GitHub → Settings → Secrets and variables → Actions"
