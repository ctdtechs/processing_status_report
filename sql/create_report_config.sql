-- =====================================================================
-- create_report_config.sql
-- ---------------------------------------------------------------------
-- Creates the single-row configuration table used by the Processing
-- Status Report tool, on the PROD SQL Server instance (same instance as
-- the abhi_mask* reporting databases).
--
-- The application (app/config_store.py) will auto-create this table and
-- seed the id=1 row on first run, so running this script by hand is
-- OPTIONAL. It is provided so a DBA can pre-create the table, review the
-- shape, and lock down permissions before the app ever connects.
--
-- Run it against the database named in the CONFIG_DB_NAME environment
-- variable (e.g. master, or a dedicated 'ops' database).
--
-- SECURITY: mail_pwd_b64 / report_pwd_b64 are BASE64 (encoding, NOT
-- encryption). Grant SELECT/UPDATE on this table only to the service
-- account that runs the report. Consider SQL Server Always Encrypted on
-- those columns if real secrecy is required.
-- =====================================================================

SET NOCOUNT ON;

IF OBJECT_ID('dbo.report_config', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.report_config (
        id              INT           NOT NULL PRIMARY KEY,
        start_date      DATE          NULL,           -- pivot range start (inclusive)
        end_date        DATE          NULL,           -- pivot range end   (exclusive)
        db_list         NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_db_list  DEFAULT '',  -- comma-separated DB names
        prod_db         NVARCHAR(256) NULL,           -- which DB in db_list is PROD (day-wise summary)
        report_server   NVARCHAR(256) NULL,           -- optional override for reporting-DB server
        report_user     NVARCHAR(256) NULL,           -- optional override for reporting-DB login
        report_pwd_b64  NVARCHAR(MAX) NULL,           -- optional override password (base64)
        from_mail       NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_from_mail DEFAULT '',
        from_name       NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_from_name DEFAULT '',
        mail_pwd_b64    NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_mail_pwd  DEFAULT '',  -- BASE64
        to_mails        NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_to        DEFAULT '',  -- ';'-separated
        cc_mails        NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_cc        DEFAULT '',  -- ';'-separated
        smtp_server     NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_smtp_srv  DEFAULT 'smtp.office365.com',
        smtp_port       INT           NOT NULL CONSTRAINT DF_report_config_smtp_port DEFAULT 587,
        triggers        NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_triggers  DEFAULT '',  -- 'HH:MM,HH:MM' 24h
        last_run_marker NVARCHAR(64)  NOT NULL CONSTRAINT DF_report_config_last_run  DEFAULT '',  -- internal de-dup
        CONSTRAINT CK_report_config_singleton CHECK (id = 1)
    );
END
GO

-- Seed the singleton row if it doesn't exist. Adjust the values, then
-- (re)run, or edit later with:  python edit_config.py
IF NOT EXISTS (SELECT 1 FROM dbo.report_config WHERE id = 1)
BEGIN
    INSERT INTO dbo.report_config
        (id, start_date, end_date, db_list, prod_db,
         from_mail, from_name, mail_pwd_b64, to_mails, cc_mails,
         smtp_server, smtp_port, triggers, last_run_marker)
    VALUES
        (1,
         NULL, NULL,
         'abhi_mask,abhi_maskv2,abhi_maskv3,abhi_maskv4,abhi_maskv5,abhi_maskv6',
         'abhi_mask',
         'nv@ctdtechs.com',
         'Processing Status Report',
         'Tml2ZU0jMzE=',              -- base64 of the seed mail password; ROTATE THIS
         'vn@ctdtechs.com',
         '',
         'smtp.office365.com',
         587,
         '09:30,13:30,18:30',
         '');
END
GO

-- =====================================================================
-- Per-database table: one row per reporting database, each with its OWN
-- date range. This is what lets every database use a different start/end.
-- The app auto-creates this too and seeds it from report_config.db_list on
-- first run, so creating it by hand is OPTIONAL.
-- =====================================================================
IF OBJECT_ID('dbo.report_databases', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.report_databases (
        db_name     NVARCHAR(256) NOT NULL PRIMARY KEY,
        start_date  DATE          NULL,   -- range start (inclusive); NULL -> fall back to global/auto
        end_date    DATE          NULL,   -- range end   (exclusive); NULL -> fall back to global/auto
        is_prod     BIT           NOT NULL CONSTRAINT DF_report_databases_prod    DEFAULT 0,
        enabled     BIT           NOT NULL CONSTRAINT DF_report_databases_enabled DEFAULT 1,
        sort_order  INT           NOT NULL CONSTRAINT DF_report_databases_sort    DEFAULT 0
    );
END
GO

-- Seed one row per database if empty. Adjust names/ranges as needed.
IF NOT EXISTS (SELECT 1 FROM dbo.report_databases)
BEGIN
    INSERT INTO dbo.report_databases (db_name, start_date, end_date, is_prod, enabled, sort_order) VALUES
        ('abhi_mask',   NULL, NULL, 1, 1, 0),
        ('abhi_maskv2', NULL, NULL, 0, 1, 1),
        ('abhi_maskv3', NULL, NULL, 0, 1, 2),
        ('abhi_maskv4', NULL, NULL, 0, 1, 3),
        ('abhi_maskv5', NULL, NULL, 0, 1, 4),
        ('abhi_maskv6', NULL, NULL, 0, 1, 5);
END
GO

-- Example: give each database its own date range (end_date is EXCLUSIVE)
-- UPDATE dbo.report_databases SET start_date='2026-07-01', end_date='2026-08-01' WHERE db_name='abhi_mask';
-- UPDATE dbo.report_databases SET start_date='2026-06-01', end_date='2026-07-01' WHERE db_name='abhi_maskv2';

-- Recommended: restrict access to the service account only, e.g.
-- GRANT SELECT, UPDATE ON dbo.report_config    TO [ABHIMASK];
-- GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.report_databases TO [ABHIMASK];
