# TRAUR / MCL Bot v0.4 — Points Shop

Same logic as v0.2: player registration, persistent private archive channel per player, upload screenshots/videos/links for point requests, owner/tier-check approval, 160 weekly limit, lifetime points, leaderboard.

## Shop and balance management
- Owner / Tier Check can use `/points_adjust` to add or subtract points from a registered player. Positive adjustments respect the 160-point weekly limit; deductions never make the balance negative. Every change is written to an audit ledger and announced in the player's private channel.
- Configure up to 25 active shop items with `/shop_item_set`; remove an item with `/shop_item_remove`; review the catalog with `/shop_items`.
- Publish the public shop message with `/shop_setup`. Players can also use `/shop` or the **Магазин** button in the main panel.
- Selecting a product creates a pending request in the player's private channel. Points are deducted atomically only when Owner / Tier Check presses **Одобрить покупку**. Rejected or unaffordable purchases do not change the balance.
- Player profiles show weekly earned points, current spendable balance, and lifetime earned points. Purchases and manual deductions do not reduce the weekly-earned counter.
- Shop items are intentionally stored in SQLite instead of source code, so the real catalog and prices can be entered from Discord without another deployment.
- Owner / administrators can use the **Админ: выдать баллы** button or `/admin_grant` to grant 1–1,000,000 points per operation without changing the 160-point weekly counter. Every grant requires a reason, is stored in the transaction ledger, and is announced in the player's private channel.
- Owner / administrators can use **Админ: выдать всем** or `/admin_grant_all` to grant the same amount to every registered player outside the weekly limit. The bot previews the recipient count and total, requires a second confirmation, logs one transaction per player, and notifies every available private channel.

## Railway deployment
1. In Railway, stop the service before database migration if there is any live data. Never run this bot from your PC and Railway at the same time.
2. Attach a **Volume** to the `traur-family-bot` service with mount path `/data`.
3. In Railway service Variables, set `DISCORD_TOKEN`, `GUILD_ID`, `REVIEWER_ROLE_ID`, `OWNER_ROLE_ID`, `CATEGORY_ID`. The volume mount path is auto-injected as `RAILWAY_VOLUME_MOUNT_PATH` on a correctly attached Railway Volume. Do not put `.env` in GitHub.
4. Upload `bot.py`, `database.py`, `requirements.txt` (and other non-secret project files) to the connected GitHub repository; Railway redeploys. The application has no HTTP port and must be configured as a worker, running `python bot.py` if Railway does not auto-detect the start command.
5. The code now uses `/data/mcl.sqlite3` on Railway, and a local `mcl.sqlite3` when run on your PC. A Volume by itself does NOT copy the PC's database. See migration section before approving real points.

## Existing database migration (critical)
- Do not delete or overwrite your local `mcl.sqlite3`; that is the original player / points data.
- Back up the source SQLite database using the SQLite backup API while the local bot is stopped. Example in the local project folder (PowerShell):
  ` .\.venv\Scripts\python.exe -c "import sqlite3; a=sqlite3.connect('mcl.sqlite3'); b=sqlite3.connect('mcl-backup.sqlite3'); a.backup(b); b.close(); a.close()" `
- Upload the resulting backup to the mounted Railway `/data/mcl.sqlite3` using a **supported authenticated** transfer method such as Railway's volume file tooling, before accepting new applications. Verify the file contents after transfer. Railway's deployment build is not a safe substitute for transferring data to a Volume.
- If a legacy `mcl.sqlite3` is present beside `database.py` and the Volume database is still absent, startup copies it with SQLite's backup API, checks the copy, and keeps the source. It never overwrites an existing Volume database. Normal GitHub deployments exclude `*.sqlite3`, so use the authenticated Volume transfer for a PC-only database.
- If your previous Railway deployment recorded any real awards in its temporary filesystem, reconcile those with the local database before switching.

## Safeguards
- At startup on Railway without a Volume or explicit `MCL_DB_PATH`, the app exits instead of silently creating an ephemeral database.
- Existing SQLite tables and rows are preserved by `CREATE TABLE IF NOT EXISTS` and v0.2 migrations.
- SQLite context-managed connections are closed after commit/rollback so the WAL can checkpoint cleanly.
- Keep one running instance to avoid duplicate Discord event handling.
- Make regular backup copies from the Volume; a Volume is not a backup.
- Do not include `.env`, `mcl.sqlite3`, `.venv`, or the database backup in Git.
