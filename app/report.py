#!/usr/bin/env python3
"""
report.py

Builds the console (ANSI) tables and the HTML email body, matching the
"Processing Status" + "Day-wise Execution Summary" two-table format.
"""

import sys
from datetime import datetime, timedelta

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
# HTML email -- professional newsletter layout. Email clients (esp. Outlook)
# ignore <style> blocks, flexbox and grid, so everything is table-based with
# inline styles and bgcolor attributes for maximum compatibility.
# ------------------------------------------------------------------------- #
# Brand palette -- red + gold, from the logo's sunburst.
_BRAND = "#c1272d"          # primary red (header / table-head)
_BRAND_DARK = "#8e1b20"     # deep red accent
_ACCENT = "#f2a900"         # gold (accent strips, section rule)
_ON_BRAND = "#fbe3b8"       # soft gold-cream text on the red header
_INK = "#2b2b2b"            # body text
_MUTED = "#8a8a8a"          # secondary text
_LINE = "#e8dcdc"           # warm table borders
_ZEBRA = "#fbf3f2"          # alternating row (warm tint)
_FONT = "Segoe UI,Roboto,Helvetica,Arial,sans-serif"

# The sign-off name. Easy to change here (or ask to make it config-driven).
SIGNATURE_NAME = "ABHI Aadhaar Support Agent"


def _is_number(value) -> bool:
    s = str(value).strip().replace(",", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _newsletter_table(headers: list, rows: list) -> str:
    """A zebra-striped, full-width HTML table with a branded header row.
    First column left-aligned (label); numeric cells right-aligned."""
    head_cell = (
        f"padding:10px 14px;background:{_BRAND};color:#ffffff;"
        f"font-family:{_FONT};font-size:13px;font-weight:600;"
        "border:1px solid " + _BRAND + ";white-space:nowrap;"
    )
    th_cells = "".join(
        f'<th align="{"left" if i == 0 else "center"}" style="{head_cell}">{h}</th>'
        for i, h in enumerate(headers)
    )

    body_rows = ""
    for r_idx, row in enumerate(rows):
        bg = _ZEBRA if r_idx % 2 else "#ffffff"
        tds = ""
        for i, cell in enumerate(row):
            text = str(cell)
            is_na = text.strip().upper() == "N/A"
            align = "left" if (i == 0 or not _is_number(cell)) else "right"
            base = (
                f"padding:9px 14px;border:1px solid {_LINE};"
                f"font-family:{_FONT};font-size:13px;background:{bg};"
            )
            if i == 0:
                base += f"font-weight:600;color:{_INK};"
            elif is_na:
                base += f"color:{_MUTED};font-style:italic;"
            else:
                base += f"color:{_INK};"
            tds += f'<td align="{align}" style="{base}">{text}</td>'
        body_rows += f"<tr>{tds}</tr>"

    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;margin:6px 0 4px;">'
        f"<thead><tr>{th_cells}</tr></thead><tbody>{body_rows}</tbody></table>"
    )


def _section_heading(text: str) -> str:
    return (
        f'<h2 style="margin:26px 0 6px;font-family:{_FONT};font-size:16px;'
        f'font-weight:700;color:{_BRAND};border-left:4px solid {_ACCENT};'
        f'padding-left:10px;">{text}</h2>'
    )


def build_email_html(
    selected_dbs: list,
    db_configs: dict,
    results: dict,
    date_ranges: dict,
    daily_rows: list,
    daily_period_label: str,
    signature_name: str = SIGNATURE_NAME,
) -> str:
    """Builds the combined HTML email body as a professional newsletter:
    a branded header, the 'Processing Status' pivot table, the
    'Day-wise <Month> Execution Summary' table, and a signed footer."""

    status_headers = ["Stage"] + [db_configs[k].label for k in selected_dbs]
    status_rows = build_status_table_rows(selected_dbs, db_configs, results, date_ranges)
    status_table_html = _newsletter_table(status_headers, status_rows)

    daily_headers = queries.DAILY_STATUS_COLUMNS
    daily_table_rows = build_daily_table_rows(daily_rows)
    daily_table_html = (
        _newsletter_table(daily_headers, daily_table_rows)
        if daily_table_rows
        else f'<p style="font-family:{_FONT};font-size:13px;color:{_MUTED};'
             'font-style:italic;margin:6px 0;">No day-wise records for this period.</p>'
    )

    now = datetime.now()
    report_date = now.strftime("%d-%b-%Y")
    generated_at = now.strftime("%d-%b-%Y %H:%M")
    db_count = len(selected_dbs)
    daily_summary_label = (f"Day-wise {daily_period_label} Execution Summary").strip()

    preheader = f"Processing Status report for {report_date} across {db_count} database(s)."

    html = f"""\
<!-- preheader (hidden in most clients) -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="width:100%;background:#ffffff;margin:0;padding:0;">

  <!-- Header banner (full-width, red with a gold accent strip) -->
  <tr>
    <td style="background:{_BRAND};border-bottom:4px solid {_ACCENT};padding:22px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-family:{_FONT};color:#ffffff;font-size:20px;font-weight:700;letter-spacing:.2px;">
            Processing Status Report
          </td>
          <td align="right" style="font-family:{_FONT};color:{_ON_BRAND};font-size:13px;white-space:nowrap;">
            {report_date}
          </td>
        </tr>
        <tr>
          <td colspan="2" style="font-family:{_FONT};color:{_ON_BRAND};font-size:12px;padding-top:4px;">
            Aadhaar masking &amp; extraction pipeline &mdash; automated daily summary
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body (directly on the main body, full width) -->
  <tr>
    <td style="padding:26px 32px 8px;">
      <p style="margin:0 0 12px;font-family:{_FONT};font-size:14px;color:{_INK};">Hi Team,</p>
      <p style="margin:0 0 4px;font-family:{_FONT};font-size:14px;color:{_INK};line-height:1.55;">
        Please find below the processing status generated on <strong>{report_date}</strong>,
        covering <strong>{db_count}</strong> database(s).
      </p>

      {_section_heading("Processing Status")}
      {status_table_html}

      {_section_heading(daily_summary_label)}
      {daily_table_html}

      <!-- Sign-off -->
      <p style="margin:28px 0 2px;font-family:{_FONT};font-size:14px;color:{_INK};">Regards,</p>
      <p style="margin:0;font-family:{_FONT};font-size:14px;font-weight:700;color:{_BRAND};">{signature_name}</p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:16px 32px 22px;border-top:2px solid {_ACCENT};">
      <p style="margin:0;font-family:{_FONT};font-size:11px;color:{_MUTED};line-height:1.5;">
        This is an automated report generated on {generated_at}. Please do not reply to this email.
      </p>
    </td>
  </tr>

</table>
"""
    return html


# ------------------------------------------------------------------------- #
# Storage & system alert email (separate from the processing-status report)
# ------------------------------------------------------------------------- #
def _fmt_gb(value) -> str:
    return f"{value:,.2f} GB" if value is not None else "N/A"


def _callout(text: str, danger: bool) -> str:
    bg = "#fdecec" if danger else "#fef7e6"
    bar = _BRAND if danger else _ACCENT
    color = _BRAND_DARK if danger else "#7a5a00"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:8px 0 4px;"><tr><td style="background:{bg};'
        f'border-left:5px solid {bar};padding:12px 16px;font-family:{_FONT};'
        f'font-size:13px;font-weight:600;color:{color};">{text}</td></tr></table>'
    )


def _size_cell(entry) -> str:
    """'12.34 GB (56 files)' from a sizes entry, or 'N/A' if missing."""
    if not entry:
        return "N/A"
    txt = _fmt_gb(entry.get("gb"))
    files = entry.get("files")
    if files is not None:
        txt += f' ({files:,} file{"s" if files != 1 else ""})'
    return txt


def build_storage_email_html(
    metrics: dict,
    sizes: dict,
    range_start,
    range_end,
    threshold_pct: float,
    over_threshold: bool,
    signature_name: str = SIGNATURE_NAME,
) -> str:
    """Newsletter-style storage + system alert email body.

    `sizes` maps 'input' / 'notfound' / 'found' -> {'gb','files','missing'},
    computed from summing on-disk sizes of the DB-referenced file paths.
    """
    host = metrics.get("hostname", "server")
    ip = metrics.get("ip", "N/A")
    mount = metrics.get("mount", "/data")
    used_pct = metrics.get("used_pct", 0.0)
    now = datetime.now()
    report_date = now.strftime("%d-%b-%Y %H:%M")
    range_note = (f"{range_start} &rarr; {range_end}"
                  if range_start and range_end else "N/A")

    # --- Server & storage (matches the requested layout) ---
    server_rows = [
        ["Server", host],
        ["IP Address", ip],
        ["Date", report_date],
        [f"Total size ({mount})", _fmt_gb(metrics.get("total_gb"))],
        ["Occupied", f'{_fmt_gb(metrics.get("occupied_gb"))} ({used_pct:.1f}%)'],
        ["Input File Size", _size_cell(sizes.get("input"))],
        ["Aadhaar not found", _size_cell(sizes.get("notfound"))],
        ["Aadhaar found", _size_cell(sizes.get("found"))],
        ["Extracted Files Size", _size_cell(sizes.get("extracted"))],
    ]
    server_table = _newsletter_table(["Server &amp; Storage", "Value"], server_rows)

    sizes_note = ""
    if sizes:
        total_missing = sum(v.get("missing", 0) for v in sizes.values())
        sizes_note = (
            f'<p style="font-family:{_FONT};font-size:11px;color:{_MUTED};margin:2px 0 0;">'
            f'File sizes summed from DB paths over {range_note}.'
            + (f' {total_missing:,} referenced file(s) not found on disk were skipped.'
               if total_missing else "")
            + "</p>"
        )

    # --- CPU ---
    load = metrics.get("load_avg")
    load_str = " / ".join(f"{x:.2f}" for x in load) if load else "N/A"
    cpu_rows = [
        ["Overall utilization", f'{metrics.get("cpu_overall_pct", 0.0):.1f}%'],
        ["Load average (1 / 5 / 15 min)", load_str],
        ["Logical cores", str(metrics.get("logical_cores") or "N/A")],
        ["Physical cores", str(metrics.get("physical_cores") or "N/A")],
    ]
    cpu_summary_table = _newsletter_table(["CPU", "Value"], cpu_rows)

    # --- Callout ---
    if over_threshold:
        callout = _callout(
            f"&#9888; {mount} usage is {used_pct:.1f}% &mdash; over the "
            f"{threshold_pct:.0f}% threshold. Free up space to avoid disruption.",
            danger=True,
        )
        title = "Storage &amp; System Alert"
    else:
        callout = _callout(
            f"{mount} usage is {used_pct:.1f}% (threshold {threshold_pct:.0f}%).",
            danger=False,
        )
        title = "Storage &amp; System Report"

    preheader = f"{mount} at {used_pct:.0f}% on {host}"

    html = f"""\
<!-- preheader (hidden in most clients) -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="width:100%;background:#ffffff;margin:0;padding:0;">

  <!-- Header banner -->
  <tr>
    <td style="background:{_BRAND};border-bottom:4px solid {_ACCENT};padding:22px 32px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-family:{_FONT};color:#ffffff;font-size:20px;font-weight:700;letter-spacing:.2px;">
            {title}
          </td>
          <td align="right" style="font-family:{_FONT};color:{_ON_BRAND};font-size:13px;white-space:nowrap;">
            {report_date}
          </td>
        </tr>
        <tr>
          <td colspan="2" style="font-family:{_FONT};color:{_ON_BRAND};font-size:12px;padding-top:4px;">
            Server: {host} ({ip}) &mdash; mount {mount}
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:22px 32px 8px;">
      {callout}

      {_section_heading("Server &amp; Storage")}
      {server_table}
      {sizes_note}

      {_section_heading("CPU Utilization")}
      {cpu_summary_table}

      <p style="margin:28px 0 2px;font-family:{_FONT};font-size:14px;color:{_INK};">Regards,</p>
      <p style="margin:0;font-family:{_FONT};font-size:14px;font-weight:700;color:{_BRAND};">{signature_name}</p>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:16px 32px 22px;border-top:2px solid {_ACCENT};">
      <p style="margin:0;font-family:{_FONT};font-size:11px;color:{_MUTED};line-height:1.5;">
        Automated storage &amp; system alert generated on {report_date}. Please do not reply to this email.
      </p>
    </td>
  </tr>

</table>
"""
    return html
