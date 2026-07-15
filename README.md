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

The application auto-creates and seeds this table on first connect, so it
works out of the box. A DBA can also pre-create it with
[sql/create_report_config.sql](sql/create_report_config.sql) and lock
down permissions first.

### Configurable fields (the config row)

| Column | Meaning |
| --- | --- |
| `start_date` | Pivot range start (inclusive). Blank → current month to date. |
| `end_date` | Pivot range end (exclusive). Blank → current month to date. |
| `db_list` | Comma-separated database names to run against. |
| `prod_db` | Which database in `db_list` is PROD (day-wise summary). |
| `report_server` / `report_user` / `report_pwd_b64` | Optional overrides for the reporting-DB connection. Blank → reuse the bootstrap `CONFIG_DB_*` login (config table is on the same instance). |
| `from_mail` | "From" mail id. |
| `from_name` | "From" display name. |
| `mail_pwd_b64` | Mail password, **base64-encoded**. |
| `to_mails` | Semicolon-separated To recipients. |
| `cc_mails` | Semicolon-separated Cc recipients. |
| `smtp_server` / `smtp_port` | SMTP host/port. |
| `triggers` | Comma-separated `HH:MM` (24h) trigger times, e.g. `09:30,13:30,18:30`. |
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
python edit_config.py show      # print current config (passwords masked)
python edit_config.py edit      # guided edit of every field
python edit_config.py mailpwd   # rotate just the mail password
```

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
