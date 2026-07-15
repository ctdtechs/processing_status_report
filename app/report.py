#!/usr/bin/env python3
"""
report.py

Builds the console (ANSI) tables and the HTML email body, matching the
"Processing Status" + "Day-wise Execution Summary" two-table format.
"""

import sys
from datetime import datetime, timedelta

from . import config_store as cs
from . import queries

# ------------------------------------------------------------------------- #
# Console colors (ANSI escape codes). Auto-disabled when stdout isn't a
# real terminal, so nothing garbled ends up in redirected output.
# ------------------------------------------------------------------------- #
COLOR_ENABLED = sys.stdout.isatty()


class C:
    RESET = "\033[0m" if COLOR_ENABLED else ""
    BOLD = "\033[1m" if COLOR_ENABLED else ""
    DIM = "\033[2m" if COLOR_ENABLED else ""
    CYAN = "\033[36m" if COLOR_ENABLED else ""
    GREEN = "\033[32m" if COLOR_ENABLED else ""
    YELLOW = "\033[33m" if COLOR_ENABLED else ""
    RED = "\033[31m" if COLOR_ENABLED else ""
    MAGENTA = "\033[35m" if COLOR_ENABLED else ""
    BLUE = "\033[34m" if COLOR_ENABLED else ""
    GRAY = "\033[90m" if COLOR_ENABLED else ""


def compute_period_label(start_date_str: str, end_date_str: str) -> str:
    """
    start_date is inclusive, end_date is exclusive (matches query semantics).
    Examples:
      2026-05-01 -> 2026-06-01   => "May 2026"
      2026-05-01 -> 2026-07-01   => "May - June 2026"
      2026-11-01 -> 2027-02-01   => "November 2026 - January 2027"
    """
    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    end = datetime.strptime(end_date_str, "%Y-%m-%d")
    last_included = end - timedelta(days=1)
    start_month, start_year = start.strftime("%B"), start.year
    end_month, end_year = last_included.strftime("%B"), last_included.year

    if (start.year, start.month) == (last_included.year, last_included.month):
        return f"{start_month} {start_year}"
    if start_year == end_year:
        return f"{start_month} - {end_month} {start_year}"
    return f"{start_month} {start_year} - {end_month} {end_year}"


# ------------------------------------------------------------------------- #
# Console table (generic grid renderer, used for both tables)
# ------------------------------------------------------------------------- #
def print_grid_table(headers: list, rows: list):
    all_rows = [headers] + rows
    col_widths = [
        max(len(str(row[i])) for row in all_rows)
        for i in range(len(headers))
    ]

    def sep_line():
        line = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
        return f"{C.GRAY}{line}{C.RESET}"

    def fmt_header():
        cells = [str(h).ljust(col_widths[i]) for i, h in enumerate(headers)]
        colored = [f"{C.CYAN}{C.BOLD}{c}{C.RESET}" for c in cells]
        return f"{C.GRAY}|{C.RESET} " + f" {C.GRAY}|{C.RESET} ".join(colored) + f" {C.GRAY}|{C.RESET}"

    def fmt_data_row(row):
        cells = []
        for i, cell in enumerate(row):
            padded = str(cell).ljust(col_widths[i])
            if i == 0:
                colored = f"{C.MAGENTA}{C.BOLD}{padded}{C.RESET}"
            elif str(cell).strip().upper() == "N/A":
                colored = f"{C.DIM}{padded}{C.RESET}"
            else:
                colored = f"{C.GREEN}{padded}{C.RESET}"
            cells.append(colored)
        return f"{C.GRAY}|{C.RESET} " + f" {C.GRAY}|{C.RESET} ".join(cells) + f" {C.GRAY}|{C.RESET}"

    print(sep_line())
    print(fmt_header())
    print(sep_line())
    for row in rows:
        print(fmt_data_row(row))
    print(sep_line())


# ------------------------------------------------------------------------- #
# "Processing Status" pivot table (Stage x Database)
# ------------------------------------------------------------------------- #
def build_status_table_rows(selected_dbs: list, db_configs: dict, results: dict, date_ranges: dict) -> list:
    table_rows = []
    for stage_label, field in queries.STAGE_ORDER:
        row = [stage_label]
        for k in selected_dbs:
            if field == "Month":
                start_date, end_date = date_ranges[k]
                val = compute_period_label(start_date, end_date)
            else:
                val = results.get(k, {}).get(field)
                val = val if val is not None else "N/A"
            row.append(val)
        table_rows.append(row)
    return table_rows


def print_status_report(selected_dbs: list, db_configs: dict, results: dict, date_ranges: dict, title: str):
    headers = ["Stage"] + [db_configs[k].label for k in selected_dbs]
    table_rows = build_status_table_rows(selected_dbs, db_configs, results, date_ranges)

    print(f"\n{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")
    print(f"{C.BLUE}{C.BOLD} {title}{C.RESET}")
    print(f"{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")
    print_grid_table(headers, table_rows)


# ------------------------------------------------------------------------- #
# "Day-wise Execution Summary" table (PROD only)
# ------------------------------------------------------------------------- #
def build_daily_table_rows(daily_rows: list) -> list:
    table_rows = []
    for r in daily_rows:
        d = r.get("Date")
        date_str = d.strftime("%d-%m-%Y") if hasattr(d, "strftime") else str(d)
        row = [date_str] + [r.get(col, 0) for col in queries.DAILY_STATUS_COLUMNS[1:]]
        table_rows.append(row)
    return table_rows


def print_daily_report(daily_rows: list, title: str):
    headers = queries.DAILY_STATUS_COLUMNS
    table_rows = build_daily_table_rows(daily_rows)

    print(f"\n{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")
    print(f"{C.BLUE}{C.BOLD} {title}{C.RESET}")
    print(f"{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")
    if not table_rows:
        print(f"{C.DIM}(no rows for this range){C.RESET}")
        return
    print_grid_table(headers, table_rows)


# ------------------------------------------------------------------------- #
# HTML email -- two tables, styled to match the reference report format.
# ------------------------------------------------------------------------- #
_HEADER_STYLE = (
    "padding:8px 12px;border:1px solid #ccc;background:#2f5597;"
    "color:#ffffff;text-align:left;font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
)
_CELL_STYLE = "padding:8px 12px;border:1px solid #ccc;font-family:Segoe UI,Arial,sans-serif;font-size:13px;"
_FIRST_CELL_STYLE = _CELL_STYLE + "font-weight:600;background:#f2f2f2;"


def _html_table(headers: list, rows: list) -> str:
    th_cells = "".join(f'<th style="{_HEADER_STYLE}">{h}</th>' for h in headers)
    body_rows = ""
    for row in rows:
        tds = "".join(
            f'<td style="{_FIRST_CELL_STYLE if i == 0 else _CELL_STYLE}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body_rows += f"<tr>{tds}</tr>"
    return (
        '<table style="border-collapse:collapse;margin:12px 0;">'
        f"<thead><tr>{th_cells}</tr></thead><tbody>{body_rows}</tbody></table>"
    )


def build_email_html(
    selected_dbs: list,
    db_configs: dict,
    results: dict,
    date_ranges: dict,
    daily_rows: list,
    daily_period_label: str,
) -> str:
    """Builds the combined HTML email body: 'Processing Status' pivot table
    followed by the 'Day-wise <Month> Execution Summary' table, matching the
    two-table layout of the reference report."""

    status_headers = ["Stage"] + [db_configs[k].label for k in selected_dbs]
    status_rows = build_status_table_rows(selected_dbs, db_configs, results, date_ranges)
    status_table_html = _html_table(status_headers, status_rows)

    daily_headers = queries.DAILY_STATUS_COLUMNS
    daily_table_rows = build_daily_table_rows(daily_rows)
    daily_table_html = _html_table(daily_headers, daily_table_rows)

    report_date = datetime.now().strftime("%d-%b-%Y")

    html = f"""\
<html>
<body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222;">
<p>Hi Team,</p>
<p>Please find below the Processing Status report generated on {report_date}.</p>
{status_table_html}
<p>Day-wise {daily_period_label} Execution Summary</p>
{daily_table_html}
<p>Regards,<br>Automated Reporting</p>
</body>
</html>
"""
    return html
