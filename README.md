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

## Клиенты через 14 дней

The `📅 Клиенты через 14 дней` panel is a separate campaign from cold-lead
follow-ups. It reads the existing sales sheet but never writes new columns or
changes a cell. Configure `CHECKIN_SHEET_CSV_URL` (a read-only CSV export of
the same live sheet), `CHECKIN_START_DATE` (the launch date) and the B/D/E
column mapping in `/etc/tg-follow-up.env`. Local Traffic does not need to be
in DialogHub: an administrator connects it once in the control bot with the
account's phone number, Telegram code and, if enabled, its 2FA password.

Every scan re-downloads the current sheet and takes only sales dated on or
after `CHECKIN_START_DATE` whose date plus 14 days falls today (and, by
default, the prior missed day). Every sale is eligible except tests, top-ups or
second instalments, and repeat payments. An optional minimum amount is changed
in the panel and is disabled initially. The
bot finds a Telegram group whose title matches the project title after case,
spaces and punctuation are normalised. It compares group members with direct
dialogs from the configured sender account. Only exactly one match enters the
ready queue.

No match, several matching people, duplicate group titles, Telegram errors or
an unavailable sender create a card for the operator instead of sending a
message. In a separate group send `/checkin_errors_here`; its cards offer
buttons to choose one of the found people, enter an existing `@username`, skip
the sale or block that project permanently. The bot records every decision in
its local SQLite database, so daily sheet re-reads cannot duplicate a message.

Delivery starts disabled. Save the agreed message text in the panel, import
the named Local Traffic account, configure the error group, inspect a sample,
then remove the global pause and explicitly enable this campaign. The global
emergency stop applies to it as well.

The campaign panel includes three approved texts by default. It sends them in
order and starts again from the first after the third; every sent row retains
the exact text used. Texts can be replaced in the panel by sending one variant
per line. For the current main sales sheet use B (`ДАТА`), D (`Описание`) and
E (`Сумма сделки`); the bot deliberately filters by the deal sum, not the
salesperson's earned amount in C. Dates without a year are treated as dates in
the current year, except a later month is treated as the previous year.

## Активные клиенты за все месяцы

The `💬 Активные клиенты · все месяцы` panel is a separate, one-time campaign.
It reads the same read-only sales sheet as the 14-day check-in, using column D
for the project description. The first enablement freezes the projects that
passed the live preview; later sales and newly active chats do not enter it.
Tests, top-ups and repeat payments are excluded. A row is mapped only when its
brand and city identify exactly one project in the reviewed client-chat map.
Unmatched rows are displayed for operator review and never sent automatically.
The operator can choose a reviewed project in the panel; that choice is tied to
the exact source row and description, so a changed row must be reviewed again.

For each mapped project, the sender checks the live client folder and unchanged
chat titles. A real incoming message from someone outside the configured team
within the last 10 days (configurable to 5) is required. The campaign sends
once to the most recently active related chat and once to one verified direct
contact from that chat. A project with VK and Avito chats gets one bundle.
The ledger also excludes contacts already sent a 14-day check-in, renewal, or
combined lost-client offer. Every chat and private send has a durable Telegram
random ID; an uncertain outcome is held for review and is never retried.
Projects that cease to qualify before sending are recorded as skipped. The
campaign turns itself off after all frozen projects are sent, skipped, or held.

Both chat and private texts start empty, and sending starts disabled. Enter the
approved texts in the panel, run the live preview, then explicitly enable the
campaign. The global emergency stop and shared Local Traffic send interval
apply. The source sheet and combined lost-client code are not changed.

## Продления: красные и белые строки


The `🔄 Продления` panel is a separate, disabled-by-default campaign for the
renewal sheet. It reads an XLSX export because the source status is the row
fill colour, which CSV cannot carry. Configure `RENEWAL_SHEET_XLSX_URL` and
the exact Local Traffic sender name as `RENEWAL_ACCOUNT_TITLE`.

The daily scan reads every month tab. It considers only customer rows where
column B is `ВК` or `Авито` and column C has a project name. A red row
(`#FF0000` or light red `#F4CCCC`) is placed in the immediate follow-up flow. A white row
(`#FFFFFF`) waits until the configured day of the next month, initially the
5th. Older white rows beyond a seven-day grace period are recorded as expired,
never automatically delivered.

For a red row, five feedback templates are already configured and rotate in
order. White rows deliberately have no default template because their message
is still to be agreed. Save one or more white templates in the panel before
any white follow-up can be sent. Unmatched people and technical errors go to a
separate Rustam group configured with `/renewal_errors_here`, with the same
choose-user, enter-username, skip and block buttons as the 14-day campaign.

## Verification

```bash
python3 -m unittest discover -s tests -v
```

## Связи и границы проекта

Проект использует историю диалогов `tg-dialog-hub` и ведёт несколько независимых сценариев повторных обращений: основную очередь, контрольные сообщения, продления, потерянных и активных клиентов. Наличие сценария в коде не означает, что он включён в конкретной конфигурации.
