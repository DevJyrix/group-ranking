# Roblox Group Horns Ranker

Automatically accepts pending group join requests and/or ranks existing members
based on whether they own any item from the **Flaming Horns series**.

## Tracked items

| Item | Asset ID |
|---|---|
| Fiery Horns of the Netherworld | `215718515` |
| Frozen Horns of the Frigid Planes | `74891470` |
| Poisoned Horns of the Toxic Wasteland | `1744060292` |
| Stormbreak Horns of the Tempest Skies | `76479271580913` |

## Auth — two credentials, two jobs

| Credential | What it does | Where to get it |
|---|---|---|
| `ROBLOX_API_KEY` | Group write actions (accept/rank) via Open Cloud | [create.roblox.com/credentials](https://create.roblox.com/credentials) |
| `ROBLOX_COOKIE` | Inventory reads for **private** accounts | Browser DevTools → `.ROBLOSECURITY` cookie |

> **Tip:** If all your members have public inventories, you can skip `ROBLOX_COOKIE`
> and rely only on layers 1 (wearing) + 2 (is-owned). The cookie only matters for
> the layer-3 collectibles scan which handles private inventories.

### Creating the Open Cloud API key

1. Go to [create.roblox.com/credentials](https://create.roblox.com/credentials)
2. **Create API Key**
3. Add **Resource**: `Group` → select your group
4. Add **Operation**: `group:read` and `group:write`
5. Copy the key into `ROBLOX_API_KEY`

The account that owns the key must be a **group owner or admin**.

## Ownership detection layers

Tried in order, short-circuits on first positive hit:

| Layer | Endpoint | Works on private inventory? |
|---|---|---|
| 1 — Wearing | `avatar.roblox.com/v1/users/{id}/avatar` | ✅ Always public |
| 2 — is-owned | `inventory.roblox.com/v1/users/{id}/items/Asset/{assetId}/is-owned` | ❌ Public only |
| 3 — Collectibles | `inventory.roblox.com/v2/users/{id}/inventory` | ✅ With admin cookie |

## Deploy to Railway

### 1. Push to GitHub

```
git init
git add .
git commit -m "horns ranker"
gh repo create horns-ranker --private --push --source .
```

### 2. Create Railway project

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repo
3. Railway auto-detects Python via nixpacks and runs `group_ranker.py`

### 3. Set environment variables

Railway → your service → **Variables**:

| Variable | Required | Description |
|---|---|---|
| `ROBLOX_API_KEY` | ✅ | Open Cloud key (group read + write) |
| `ROBLOX_COOKIE` | Recommended | `.ROBLOSECURITY` for private inventory checks |
| `GROUP_ID` | ✅ | Your group's numeric ID |
| `HORNS_RANK_ID` | ✅ if `RANK_MEMBERS=true` | Role ID to assign horn owners |
| `ACCEPT_PENDING` | | `true` / `false` (default: `true`) |
| `RANK_MEMBERS` | | `true` / `false` (default: `true`) |
| `DECLINE_NON_OWNERS` | | `true` / `false` (default: `true`) |
| `DRY_RUN` | | `true` to log without acting |
| `DISCORD_WEBHOOK` | | Optional run summary |

### 4. Add a cron trigger

Railway → service → **Settings** → **Triggers** → **Add Cron**

| How often | Cron expression |
|---|---|
| Every 30 min | `*/30 * * * *` |
| Every hour | `0 * * * *` |
| Every 6 hours | `0 */6 * * *` |

Railway spins up the container, runs the script, shuts down. You pay for seconds.

### 5. Get your HORNS_RANK_ID

Open this in your browser (replace `YOUR_GROUP_ID`):
```
https://groups.roblox.com/v1/groups/YOUR_GROUP_ID/roles
```
Find the role you want to assign horn owners and copy its `"id"` field.

## Local test run

```bash
pip install requests
DRY_RUN=true \
ROBLOX_API_KEY="your-key-here" \
ROBLOX_COOKIE="your-cookie-here" \
GROUP_ID=12345 \
HORNS_RANK_ID=67890 \
python group_ranker.py
```

Always test with `DRY_RUN=true` first — it logs exactly what it would do without touching anything.

## Performance — caching

The script uses a local `.horns_cache.json` file to track members already checked. This means:

- **First run**: Checks all pending requests and all unranked members
- **Subsequent runs**: Only checks new pending requests and new members who joined since the last run
- **Members who already got ranked**: Skipped (not re-checked)
- **Members who haven't bought horns yet**: Cached after first check, skipped unless they change rank

This makes the script super fast on repeated runs (every 5-10 minutes), while still catching new joins instantly.

The cache resets if a member's rank changes (e.g., they buy horns and get promoted), so they'll be re-checked next run.

## Instant ranking — watcher service

A lightweight **watcher** service continuously polls pending requests every **~10 seconds** and instantly accepts/ranks horn owners the moment they join. No waiting for cron jobs.

**How it works:**
1. Watcher checks pending list every 10 sec
2. Detects new join request
3. Checks if they own horns
4. If yes → **instantly accepts + ranks** (within 10-30 sec of joining)
5. If no → declines and moves on

**Setup on Railway (recommended):**
1. In your Railway project, go to Settings → Service
2. Choose **Generate from Procfile** or create a new service with type "Worker"
3. Select `watcher: python watcher.py` from the Procfile
4. Set the same environment variables (ROBLOX_API_KEY, GROUP_ID, HORNS_RANK_ID, etc.)
5. Deploy

Now you have two services running in parallel:
- **`worker`** — batch cron job (ranks all members periodically)
- **`watcher`** — instant service (accepts new horn owners as they join)

Combined, they provide **instant acceptance** + **comprehensive coverage**.

## GitHub Actions (recommended alternative)

You can run the ranker directly from GitHub Actions on a schedule (no Railway required). I added
`.github/workflows/schedule.yml` which runs on `push` and hourly by default.

Steps to enable it:

1. Create a GitHub repository and push this code.

```
git init
git add .
git commit -m "horns ranker"
gh repo create <your-repo-name> --private --push --source .
```

2. In your GitHub repo: Settings → Secrets and variables → Actions → New repository secret.
	Add the following secrets (do NOT paste secrets into chat):

	- `ROBLOX_API_KEY` (required)
	- `ROBLOX_COOKIE` (recommended for private inventories)
	- `GROUP_ID` (required)
	- `HORNS_RANK_ID` (required if `RANK_MEMBERS=true`)
	- Optional: `ACCEPT_PENDING`, `RANK_MEMBERS`, `DECLINE_NON_OWNERS`, `DRY_RUN`, `DISCORD_WEBHOOK`

3. Confirm Actions are enabled for your repository and the workflow will start on the next schedule.

Security note: never share your `.ROBLOSECURITY` cookie or API keys publicly. I cannot accept or store secrets — add them directly into GitHub or Railway.

## Helper scripts

Three PowerShell helpers are provided in the `scripts/` folder to make setup easier. Run them locally — they will prompt you for values and do not upload secrets for you.

- `scripts/create_env.ps1` — interactively create a local `.env` file (this file is ignored by git).
- `scripts/push_and_create_repo.ps1` — initialize git, create the GitHub repo via `gh` and push the `main` branch (or set remote and push if `gh` is not available).
- `scripts/set_github_secrets.ps1` — interactively set repository secrets using `gh secret set` (you must run `gh auth login` first).

Example usage (PowerShell):
```powershell
# create a local .env (never commit)
.\scripts\create_env.ps1

# create GitHub repo and push (adjust repo arg if you like)
.\scripts\push_and_create_repo.ps1 -Repo DevJyrix/group-ranking -Private

# add secrets to GitHub (prompts for each)
.\scripts\set_github_secrets.ps1
```

Security reminder: never commit `.env` or paste secrets into issues or chat. Use GitHub Secrets and Railway Variables only.

## Deploying to Railway

If you prefer Railway, follow the existing steps above. Do not paste secrets into issue trackers or chat; instead set them in Railway's Environment variables UI for your project.

