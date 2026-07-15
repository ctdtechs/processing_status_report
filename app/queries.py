"""
queries.py

All SQL used by the Processing Status Report, kept separate from
connection/retry/formatting logic.
"""

# ------------------------------------------------------------------------- #
# Optimized "Processing Status" pivot query -- single pass per source table
# via temp tables. Returns TWO result sets:
#   1) documents/files-derived counts
#   2) extractionDetails-derived counts
# Unchanged from the original optimization; only moved into its own module.
# ------------------------------------------------------------------------- #
PROCESSING_STATUS_SQL = """
SET NOCOUNT ON;
SET LOCK_TIMEOUT {lock_timeout_ms};
-- Reporting query: don't block on / wait for locks held by concurrent
-- writers. Trades strict consistency for speed (may read uncommitted /
-- in-flight rows) -- acceptable for a status dashboard, not for anything
-- requiring exact transactional accuracy.
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

DECLARE @StartDate DATE = ?;
DECLARE @EndDate   DATE = ?;

IF OBJECT_ID('tempdb..#Files') IS NOT NULL DROP TABLE #Files;
IF OBJECT_ID('tempdb..#Docs')  IS NOT NULL DROP TABLE #Docs;

SELECT id, processing_status, Upload_Status
INTO #Files
FROM dbo.files
WHERE uploaded_at >= @StartDate AND uploaded_at < @EndDate;

CREATE UNIQUE CLUSTERED INDEX IX_tmp_files_id ON #Files(id);

SELECT DownloadStatus
INTO #Docs
FROM dbo.documents
WHERE UploadDate >= @StartDate AND UploadDate < @EndDate;

-- Result set 1: documents + files derived stages (single scan each)
SELECT
    (SELECT COUNT(*) FROM #Docs)                                             AS TotalRecords,
    (SELECT COUNT(*) FROM #Docs  WHERE DownloadStatus = 'Downloaded')        AS Downloaded,
    (SELECT COUNT(*) FROM #Docs  WHERE DownloadStatus = 'Yet to Download')   AS DownloadPending,
    (SELECT COUNT(*) FROM #Files WHERE processing_status = 'Queued')         AS ExtractionCompleted,
    (SELECT COUNT(*) FROM #Files WHERE processing_status = 'Completed')      AS ZipCreationCompleted,
    (SELECT COUNT(*) FROM #Files WHERE Upload_Status = 'Completed')          AS UploadCompleted;

-- Result set 2: extractionDetails derived stages (single join, single scan)
SELECT
    SUM(CASE WHEN ed.identificationStatus IN ('Completed','Failed') THEN 1 ELSE 0 END) AS IdentificationCompleted,
    SUM(CASE WHEN ed.maskingStatus = 'Aadhar found' THEN 1 ELSE 0 END)                 AS AadhaarMaskedCount,
    SUM(CASE WHEN ed.processingStatus = 'Completed' THEN 1 ELSE 0 END)                 AS MiddlewareCompleted,
    SUM(CASE WHEN ed.outputFilePrepration = 'Completed' THEN 1 ELSE 0 END)             AS OutputCreationCompleted
FROM #Files f
JOIN dbo.extractionDetails ed ON ed.fileId = f.id;

DROP TABLE #Files;
DROP TABLE #Docs;
"""

STAGE_ORDER = [
    ("Month", "Month"),
    ("Total Records (Unique)", "TotalRecords"),
    ("Download Pending", "DownloadPending"),
    ("Downloaded", "Downloaded"),
    ("Extraction Completed", "ExtractionCompleted"),
    ("Identification Completed", "IdentificationCompleted"),
    ("Aadhaar Masked Count", "AadhaarMaskedCount"),
    ("Output Creation Completed", "OutputCreationCompleted"),
    ("Zip Creation Completed", "ZipCreationCompleted"),
    ("Upload Completed", "UploadCompleted"),
]


# ------------------------------------------------------------------------- #
# "Day-wise Execution Summary" (PROD only) -- this is the user-supplied
# query, kept logically IDENTICAL (same stage definitions, same join),
# just parameterized (no hardcoded start date) and given a LOCK_TIMEOUT /
# READ UNCOMMITTED hint so it behaves consistently with the other query
# under load instead of risking the same blocking issue described above.
# ------------------------------------------------------------------------- #
DAILY_STATUS_SQL = """
SET NOCOUNT ON;
SET LOCK_TIMEOUT {lock_timeout_ms};
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

DECLARE @StartDate DATE = ?;
DECLARE @EndDate   DATE = ?;  -- exclusive upper bound

SELECT
    CAST(B.uploaded_at AS DATE) AS [Date],
    COUNT(*) AS [Total],

    SUM(CASE
            WHEN LOWER(A.identificationStatus) NOT LIKE '%yet%'
            THEN 1 ELSE 0
        END) AS [ID Completed],

    SUM(CASE
            WHEN LOWER(A.identificationStatus) LIKE '%yet%'
            THEN 1 ELSE 0
        END) AS [ID Pending],

    SUM(CASE
            WHEN A.processingStatus = 'Completed'
            THEN 1 ELSE 0
        END) AS [Aadhaar Found],

    SUM(CASE
            WHEN A.outputFilePrepration = 'Completed'
            THEN 1 ELSE 0
        END) AS [Output Completed],

    SUM(CASE
            WHEN A.processingStatus = 'Completed'
             AND B.Upload_Status = 'Completed'
             AND B.processing_status = 'Completed'
            THEN 1 ELSE 0
        END) AS [Upload Completed]

FROM dbo.extractionDetails A
INNER JOIN dbo.files B
    ON A.fileId = B.id

WHERE CAST(B.uploaded_at AS DATE) >= @StartDate
  AND CAST(B.uploaded_at AS DATE) <  @EndDate

GROUP BY CAST(B.uploaded_at AS DATE)
ORDER BY CAST(B.uploaded_at AS DATE);
"""

DAILY_STATUS_COLUMNS = [
    "Date", "Total", "ID Completed", "ID Pending",
    "Aadhaar Found", "Output Completed", "Upload Completed",
]


# ------------------------------------------------------------------------- #
# Storage-alert file PATHS. The alert sums the on-disk size of every path
# these return (the app stats each file). Range is [@StartDate, @EndDate) to
# match the rest of the app (index-friendly; avoids CAST(... AS DATE) per row).
#
#   INPUT     -> files.file_path where processing_status = 'queued'
#   NOTFOUND  -> extractionDetails.{extractedFilePath, pickleInputPath,
#                pickleOutputPath} where outputFilePrepration in
#                ('not applicable','aadhaar not found')
#   FOUND     -> same three columns where outputFilePrepration = 'completed'
# ------------------------------------------------------------------------- #
_STORAGE_SQL_HEAD = """
SET NOCOUNT ON;
SET LOCK_TIMEOUT {lock_timeout_ms};
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

DECLARE @StartDate DATE = ?;
DECLARE @EndDate   DATE = ?;  -- exclusive upper bound
"""

STORAGE_INPUT_PATHS_SQL = _STORAGE_SQL_HEAD + """
SELECT file_path
  FROM dbo.files
 WHERE LOWER(processing_status) = 'queued'
   AND uploaded_at >= @StartDate AND uploaded_at < @EndDate;
"""

STORAGE_NOTFOUND_PATHS_SQL = _STORAGE_SQL_HEAD + """
SELECT ed.extractedFilePath, ed.pickleInputPath, ed.pickleOutputPath
  FROM dbo.extractionDetails ed
 WHERE LOWER(ed.outputFilePrepration) IN ('not applicable','aadhaar not found')
   AND ed.fileId IN (SELECT id FROM dbo.files
                      WHERE uploaded_at >= @StartDate AND uploaded_at < @EndDate);
"""

STORAGE_FOUND_PATHS_SQL = _STORAGE_SQL_HEAD + """
SELECT ed.extractedFilePath, ed.pickleInputPath, ed.pickleOutputPath
  FROM dbo.extractionDetails ed
 WHERE LOWER(ed.outputFilePrepration) = 'completed'
   AND ed.fileId IN (SELECT id FROM dbo.files
                      WHERE uploaded_at >= @StartDate AND uploaded_at < @EndDate);
"""
