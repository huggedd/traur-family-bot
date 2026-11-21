# TRAUR / MCL Bot v0.3 — Railway SQLite Volume

Same logic as v0.2: player registration, persistent private archive channel per player, upload screenshots/videos/links for point requests, owner/tier-check approval, 160 weekly limit, lifetime points, leaderboard.

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
- If your previous Railway deployment recorded any real awards in its temporary filesystem, reconcile those with the local database before switching.

## Safeguards
- At startup on Railway without a Volume or explicit `MCL_DB_PATH`, the app exits instead of silently creating an ephemeral database.
- Existing SQLite tables and rows are preserved by `CREATE TABLE IF NOT EXISTS` and v0.2 migrations.
- Keep one running instance to avoid duplicate Discord event handling.
- Make regular backup copies from the Volume; a Volume is not a backup.
- Do not include `.env`, `mcl.sqlite3`, `.venv`, or the database backup in Git.
