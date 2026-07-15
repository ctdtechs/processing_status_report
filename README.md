# Processing Status Report

## What changed from the original single-file script

1. **Email format now matches the reference report** — two tables:
   - *Processing Status* (Stage × Database pivot — unchanged logic)
   - *Day-wise `<Month>` Execution Summary* — new, built from the
     day-wise SQL you supplied, run automatically against **PROD** for
     the current month to date every time you run the report. No extra
     prompts needed; it just appears alongside the pivot table in both
     the console output and the emailed HTML.

2. **No more hardcoded credentials.** Server/database/username/password
   per database, and the SMTP mail settings, used to sit in plaintext
   (or weak base64) directly in the `.py` file. They now live in a local
   **encrypted configuration store**:
   - `config_store.sqlite3` — the configuration tables (`db_config`,
     `mail_config`). Passwords are stored **encrypted**, never plaintext.
   - `config_store.key` — the encryption key (Fernet/AES). Auto-created
     with owner-only file permissions (`chmod 600`).

   Both files are created and seeded automatically the first time you
   run anything (so it works out of the box with your existing values).
   After that, use `edit_config.py` to add a database, rotate a
   password, or change mail settings — no code edits required.

   **Keep `config_store.key` and `config_store.sqlite3` out of version
   control** (already covered by `.gitignore`). If you have a real
   secrets manager available (Azure Key Vault, AWS Secrets Manager,
   etc.), that's a better fit for a larger production estate — swap
   `config_store.py`'s `load_db_configs()` / `load_mail_config()` for
   calls into that instead; every other module only depends on the
   `DbConfig`/`MailConfig` shapes, not on SQLite specifically.

## Folder structure

```
processing_status_report/
├── status_report.py       # Main CLI -- run this
├── edit_config.py         # CLI to view/add/update/remove DB & mail config
├── requirements.txt
├── README.md
├── .gitignore
├── app/                   # Package: importable modules
│   ├── __init__.py
│   ├── config_store.py    # Encrypted config storage (SQLite + Fernet)
│   ├── db.py               # Connections, retry/backoff on transient SQL errors
│   ├── queries.py          # All SQL (pivot query + day-wise PROD query)
│   ├── report.py           # Console table rendering + HTML email building
│   └── mailer.py           # SMTP sending
└── config/                 # Created/used at runtime -- NOT source code
    ├── config_store.sqlite3   # generated on first run (git-ignored)
    └── config_store.key       # generated on first run (git-ignored)
```

Run everything from the `processing_status_report/` folder (so the
`app` package resolves and `config/` lands in the right place).

## Setup

```bash
pip install -r requirements.txt
```

First run seeds `config_store.sqlite3` from your original values
(server `10.21.42.17,1433`, user `ABHIMASK`, the 6 `abhi_mask*`
databases, `abhi_mask` flagged as PROD). Review/rotate them:

```bash
python3 edit_config.py list
python3 edit_config.py mail
```

## Run

```bash
python3 status_report.py
```

Pick database(s) and a date range for the *Processing Status* table as
before. The *Day-wise Execution Summary* for PROD (current month to
date) runs automatically and prints/emails alongside it.

## Adding a new historical database (e.g. `abhi_maskv7`)

```bash
python3 edit_config.py add
```

No code changes needed — it'll show up in the database picker next run.

## Recommended indexes

Same as before — ask your DBA to add these if not already present:

```sql
CREATE NONCLUSTERED INDEX IX_files_uploaded_at
    ON dbo.files (uploaded_at) INCLUDE (id, processing_status, Upload_Status);

CREATE NONCLUSTERED INDEX IX_documents_uploaddate
    ON dbo.documents (UploadDate) INCLUDE (DownloadStatus);

CREATE NONCLUSTERED INDEX IX_extractionDetails_fileId
    ON dbo.extractionDetails (fileId)
    INCLUDE (identificationStatus, maskingStatus, processingStatus, outputFilePrepration);
```
