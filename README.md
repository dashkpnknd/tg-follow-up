# tg-follow-up

Safe Telegram follow-up bot for existing DialogHub accounts. It imports account records without sending anything, analyses dialog histories, keeps an idempotent local ledger, and defaults to a global emergency pause.

## What is implemented

- DialogHub account import from its SQLite registry; import does not connect to or send from any account.
- Conservative classification: application markers `@gelikky` / `@LocalTraffic`, editable refusal words, any client reply, 48-hour threshold, blacklist, and prior `Фиксирую отказ?` in Telegram history.
- Durable SQLite queue, per-dialog state, blacklist and audit trail.
- A mandatory final preflight that reads the dialog again before an individual send.
- FloodWait and Telegram errors are recorded without retries intended to evade Telegram restrictions.
- Telegram control bot: import, scan, preview, status, task drafts and global emergency stop.

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

Set `DIALOGHUB_DB_PATH=/root/github-sync/tg-dialog-hub/data/dialoghub.sqlite3` and `ACCOUNT_SESSIONS_DIR=/root/FULL_CRM/accounts` for the current server layout.

## Verification

```bash
python3 -m unittest discover -s tests -v
```
