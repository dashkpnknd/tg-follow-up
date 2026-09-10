# tg-follow-up

Safe Telegram follow-up bot for existing DialogHub accounts. It imports account records without sending anything, analyses dialog histories, keeps an idempotent local ledger, and defaults to a global emergency pause.

## What is implemented

- DialogHub account import from its SQLite registry. It takes a SQLite backup of each Pyrogram session into the bot's own `sessions/` directory; it does not connect to or send from an account during import.
- Conservative classification: application markers `@gelikky` / `@LocalTraffic`, editable refusal words, any client reply, 48-hour threshold, blacklist, and prior `Фиксирую отказ?` in Telegram history.
- Durable SQLite queue, per-dialog state, blacklist and audit trail.
- A mandatory final preflight that reads the dialog again before an individual send.
- FloodWait and Telegram errors are recorded without retries intended to evade Telegram restrictions.
- Telegram control panel: accounts, dialog/candidate preview, queue and task drafts, exclusions/blacklist, editable stop-words and template, statistics, logs, delivery limits, and global emergency stop.

Sending remains disabled (`delivery_enabled=0`) and paused (`global_paused=1`) after installation. A task snapshots the candidates found at creation time; it never absorbs new candidates automatically. The worker only evaluates enabled tasks and still performs the final preflight immediately before each send. Use `/new_task`, `/tasks` and `/enable_task` only after an operator has explicitly lifted both global protections for a reviewed test sample.

## Install on the server

```bash
git clone git@github.com:dashkpnknd/tg-follow-up.git /opt/tg-follow-up
cd /opt/tg-follow-up
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo install -m 600 /dev/null /etc/tg-follow-up.env
# Fill /etc/tg-follow-up.env from .env.example. Do not put secrets in Git.
sudo cp systemd/tg-follow-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-follow-up
```

Set `DIALOGHUB_DB_PATH=/opt/dialoghub/data/dialoghub.sqlite3`, `ACCOUNT_SESSIONS_DIR=/root/FULL_CRM/accounts`, and `ACCOUNT_TITLE_PREFIX=Даниил` for the current server layout. The import rejects all other account labels.

## Access

The default `ACCESS_MODE=group_admins` uses an administration group: add the bot to that group as an administrator and set its numeric group ID as `ADMIN_CHAT_ID` in `/etc/tg-follow-up.env`. Every current administrator of that group will automatically have access from a private chat with the bot. To grant management access to every person with the bot link, set `ACCESS_MODE=public` instead; this intentionally makes all control-panel actions public.

## Verification

```bash
python3 -m unittest discover -s tests -v
```
