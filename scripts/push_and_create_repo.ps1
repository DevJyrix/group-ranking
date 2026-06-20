param(
  [string]$Repo = 'DevJyrix/group-ranking',
  [switch]$Private
)

Write-Host "Preparing git repository and pushing to GitHub: $Repo"

if (-not (Test-Path .git)) {
  git init
}

git add .
try { git commit -m "Initial commit: horns ranker" } catch { }
git branch -M main

# If gh is available, create repo and push
$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($gh) {
  $isPrivate = if ($Private) { '--private' } else { '--public' }
  Write-Host "Using gh to create repo and push. You may be prompted to login."
  gh repo create $Repo $isPrivate --source . --push --remote origin
} else {
  Write-Host "gh CLI not found. Setting remote URL and pushing (requires git remote credentials)."
  git remote remove origin 2>$null
  git remote add origin "https://github.com/$Repo.git"
  git push -u origin main
}

Write-Host "Done. Visit: https://github.com/$Repo"
