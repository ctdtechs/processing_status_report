# Processing Status Report

## What this is

Console tool that runs the *Processing Status* pivot query (Stage ×
Database) and the *Day-wise Execution Summary* against the PROD database,
renders both as console tables, and emails them as a single HTML report.

## Configuration lives in MSSQL now (not SQLite)

All configuration lives in **one row** of a table on the **Prod SQL
Server** instance:

```
dbo.report_config   (id = 1, singleton)
```

Two tables are used (both auto-created and seeded on first connect, so it
works out of the box). A DBA can also pre-create them with
[sql/create_report_config.sql](sql/create_report_config.sql) and lock
down permissions first.

### `dbo.report_databases` — one row per database, each with its own date range

| Column | Meaning |
| --- | --- |
| `db_name` | Database name to run the report against. |
| `start_date` | This DB's range start (inclusive). `NULL` → fall back to the global default, then current month to date. |
| `end_date` | This DB's range end (**exclusive**). `NULL` → fall back. |
| `is_prod` | `1` for the PROD database (drives the day-wise summary). Only one should be `1`. |
| `enabled` | `1` to include it in runs, `0` to skip. |
| `sort_order` | Column order in the report. |

This is where **different date ranges per database** live — see
[Per-database date ranges](#per-database-date-ranges) below.

### `dbo.report_config` — one row (id = 1): mail, triggers, global defaults

| Column | Meaning |
| --- | --- |
| `start_date` / `end_date` | **Default** range used only for databases whose own range is unset. Blank → current month to date. |
| `report_server` / `report_user` / `report_pwd_b64` | Optional overrides for the reporting-DB connection. Blank → reuse the bootstrap `CONFIG_DB_*` login (config table is on the same instance). |
| `from_mail` | "From" mail id. |
| `from_name` | "From" display name. |
| `mail_pwd_b64` | Mail password, **base64-encoded**. |
| `to_mails` | Semicolon-separated To recipients. |
| `cc_mails` | Semicolon-separated Cc recipients. |
| `smtp_server` / `smtp_port` | SMTP host/port. |
| `triggers` | Comma-separated `HH:MM` (24h) trigger times, e.g. `09:30,13:30,18:30`. |
| `db_list` / `prod_db` | Legacy — only used to seed `report_databases` on first run. Not read afterwards. |
| `last_run_marker` | Internal — de-dups scheduler runs. Don't edit. |

> **Security note:** `mail_pwd_b64` / `report_pwd_b64` are **base64 —
> encoding, not encryption**. Anyone who can read the row can decode the
> password. Restrict the table with SQL Server permissions (grant
> SELECT/UPDATE only to the service account), and consider SQL Server
> Always Encrypted on those columns if real secrecy is required.

### Bootstrap connection (environment variables)

The config table is on the Prod instance, so its connection details can't
live in the table. Provide them via these variables:

| Variable | Example |
| --- | --- |
| `CONFIG_DB_SERVER` | `10.21.42.17,7865` |
| `CONFIG_DB_NAME` | `master` (or a dedicated `ops` DB) |
| `CONFIG_DB_USER` | `ABHIMASK` |
| `CONFIG_DB_PWD` | `***` |
| `CONFIG_DB_DRIVER` | *(optional)* `ODBC Driver 18 for SQL Server` |

**Easiest: a `.env` file** (loaded automatically, no extra dependency).
Copy the template and fill it in:

```bash
cp .env.example .env
# then edit .env:
#   CONFIG_DB_SERVER=10.21.42.17,7865
#   CONFIG_DB_NAME=master
#   CONFIG_DB_USER=ABHIMASK
#   CONFIG_DB_PWD=your-password
```

`.env` lives in the project root (next to `status_report.py`) and is
git-ignored. Point elsewhere with `CONFIG_ENV_FILE=/path/to/env`. Real
shell environment variables, if set, take precedence over `.env`.

**Or export them in the shell** — Linux/bash:

```bash
export CONFIG_DB_SERVER="10.21.42.17,7865"
export CONFIG_DB_NAME="master"
export CONFIG_DB_USER="ABHIMASK"
export CONFIG_DB_PWD="your-password"
```

PowerShell:

```powershell
$env:CONFIG_DB_SERVER = "10.21.42.17,7865"
$env:CONFIG_DB_NAME   = "master"
$env:CONFIG_DB_USER   = "ABHIMASK"
$env:CONFIG_DB_PWD    = "***"
```

> **cron note:** cron jobs don't inherit your login shell's exports, so a
> `.env` file is the reliable choice for scheduled runs. (The app loads it
> regardless of how it's launched.)

The same login is reused to query each database in `db_list` (they're on
the same instance) unless overridden by `report_server`/`report_user`/
`report_pwd_b64`.

## Folder structure

```
processing_status_report/
├── status_report.py       # Main CLI -- run this
├── edit_config.py         # View/edit the MSSQL config row
├── requirements.txt
├── README.md
├── sql/
│   └── create_report_config.sql   # optional manual DDL for DBAs
└── app/
    ├── config_store.py    # MSSQL config storage (dbo.report_config)
    ├── db.py              # Connections, retry/backoff on transient SQL errors
    ├── queries.py         # All SQL (pivot query + day-wise PROD query)
    ├── report.py          # Console table rendering + HTML email building
    └── mailer.py          # SMTP sending
```

## Setup

```bash
pip install -r requirements.txt   # needs the ODBC Driver 18 for SQL Server installed
```

## View / edit config

```bash
python edit_config.py show      # print databases + their ranges, and mail settings
python edit_config.py edit      # guided edit of mail / global settings
python edit_config.py mailpwd   # rotate just the mail password
```

## Per-database date ranges

Each database has its **own** `start_date` / `end_date`. Set them with the
`db-*` commands (or plain SQL). `end_date` is **exclusive**.

```bash
python edit_config.py db-list                              # show each DB + its range
python edit_config.py db-set  abhi_mask   2026-07-01 2026-08-01   # July 2026
python edit_config.py db-set  abhi_maskv2 2026-06-01 2026-07-01   # June 2026
python edit_config.py db-set  abhi_maskv3 - -                     # clear -> use default/auto
python edit_config.py db-add  abhi_maskv7 2026-01-01 2026-02-01   # add a new DB
python edit_config.py db-prod abhi_mask                           # mark PROD (day-wise summary)
python edit_config.py db-remove abhi_maskv6
```

Or straight SQL:

```sql
UPDATE dbo.report_databases SET start_date='2026-07-01', end_date='2026-08-01' WHERE db_name='abhi_mask';
UPDATE dbo.report_databases SET start_date='2026-06-01', end_date='2026-07-01' WHERE db_name='abhi_maskv2';
```

**Range resolution per database** — each bound resolves independently:
- **start** = the DB's own `start_date` → global default start → 1st of the current month.
- **end** = the DB's own `end_date` → global default end → **tomorrow** (exclusive, so it includes today).

**Want a rolling "up to today" end?** Leave `end_date` **blank (NULL)** —
the app fills in today automatically on every run:

```bash
python edit_config.py db-set abhi_mask 2026-07-01 -    # from 2026-07-01 through today, rolling
```

> **Don't** put `GETDATE()` in `end_date`. It's a `DATE` column, so it
> stores a fixed value (frozen to the day you set it, not re-evaluated),
> and because the bound is *exclusive* it would drop today's rows. A blank
> `end_date` is the correct "today" behavior.

## Run

**Interactive (manual, ad-hoc)** — pick databases and a date range as before:

```bash
python status_report.py
```

**Config-driven, scheduled** — reads everything from the config row and
emails the configured recipients, no prompts:

```bash
python status_report.py --auto     # sends only if a configured trigger time has just arrived
python status_report.py --force    # sends immediately, ignoring trigger times
python status_report.py --auto --grace 30   # widen the catch-up window (minutes)
```

### The send times live in the table, not in cron

You schedule cron yourself, but the **actual mail send times come from the
`triggers` column** in `dbo.report_config` — so you can change them any
time with `edit_config.py` (or plain SQL) and never touch crontab.

How it works: cron runs `--auto` every few minutes. Each run checks the
`triggers` times against the clock and sends the report when a time has
**just arrived** — specifically within `[trigger_time, trigger_time +
grace]` (default grace 15 min), and only **once per trigger per day**
(de-duped via `last_run_marker`). It fires at/just-after the time, never
early.

**cron** — run the checker every 10 minutes:

```
*/10 * * * *  cd /path/to/processing_status_report && python status_report.py --auto
```

Keep `--grace` **>= your cron interval** so a trigger is never skipped
between two runs (10-min cron → grace 15 is safe).

**Changing the send times later** — no cron edit needed:

```bash
python edit_config.py edit    # set Trigger times, e.g. 09:30,13:30,18:30
# or straight SQL:
# UPDATE dbo.report_config SET triggers = '08:00,12:00,17:00' WHERE id = 1;
```

If `start_date`/`end_date` are both set, `--auto` also refuses to run
outside that active date range.

**Windows Task Scheduler** (if you use it instead of cron) — one task
running every ~10 min, `Program: python`, `Arguments:
C:\path\to\status_report.py --auto`, with the `CONFIG_DB_*` env vars set
for the task's service account.

## Recommended indexes

Ask your DBA to add these if not already present:

```sql
CREATE NONCLUSTERED INDEX IX_files_uploaded_at
    ON dbo.files (uploaded_at) INCLUDE (id, processing_status, Upload_Status);

CREATE NONCLUSTERED INDEX IX_documents_uploaddate
    ON dbo.documents (UploadDate) INCLUDE (DownloadStatus);

CREATE NONCLUSTERED INDEX IX_extractionDetails_fileId
    ON dbo.extractionDetails (fileId)
    INCLUDE (identificationStatus, maskingStatus, processingStatus, outputFilePrepration);
```
