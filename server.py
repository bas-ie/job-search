"""Flask server for browsing and triaging job search results."""

import json
from datetime import datetime, timedelta
from html import escape

from flask import Flask, jsonify, request

from config import FLAG_LABEL, FLAG_TOOLTIP, RECENT_CUTOFF_DAYS
from db import (
    delete_role_note,
    discard_company,
    get_all_role_notes,
    get_jobs,
    set_status,
    upsert_role_note,
)

app = Flask(__name__)


@app.route("/")
def index():
    return render_page()


@app.post("/api/status")
def update_status():
    data = request.get_json()
    job_id = data.get("id")
    status = data.get("status")
    if not job_id or status not in ("saved", "discarded", "new"):
        return jsonify({"error": "Invalid request"}), 400
    set_status([int(job_id)], status)
    return jsonify({"ok": True})


@app.post("/api/notes")
def save_note():
    data = request.get_json() or {}
    try:
        job_id = int(data.get("job_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "job_id is required"}), 400
    note = (data.get("note") or "").strip()
    if not note:
        return jsonify({"error": "note is required"}), 400
    if not upsert_role_note(job_id, note):
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({"ok": True, "job_id": job_id, "note": note})


@app.post("/api/companies/discard")
def discard_company_endpoint():
    data = request.get_json() or {}
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    _, affected = discard_company(company)
    return jsonify({"ok": True, "affected": affected})


@app.delete("/api/notes")
def remove_note():
    data = request.get_json() or {}
    try:
        job_id = int(data.get("job_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "job_id is required"}), 400
    delete_role_note(job_id)
    return jsonify({"ok": True, "job_id": job_id})


def relative_time(date_posted: str | None, first_seen: str | None) -> tuple[str, str]:
    """Return (display, tooltip) for the posted-date cell."""
    now = datetime.now()

    posted_dt = None
    if date_posted:
        try:
            posted_dt = datetime.strptime(date_posted[:10], "%Y-%m-%d")
        except ValueError:
            pass

    first_seen_dt = None
    if first_seen:
        try:
            first_seen_dt = datetime.fromisoformat(first_seen)
        except ValueError:
            pass

    if posted_dt and posted_dt.date() == now.date() and first_seen_dt:
        delta = now - first_seen_dt
        hours = int(delta.total_seconds() // 3600)
        if hours <= 0:
            minutes = max(0, int(delta.total_seconds() // 60))
            display = "just now" if minutes == 0 else f"{minutes} min ago"
        else:
            display = f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif posted_dt:
        days = (now.date() - posted_dt.date()).days
        if days < 0:
            display = posted_dt.strftime("%Y-%m-%d")
        elif days == 0:
            display = "today"
        elif days == 1:
            display = "yesterday"
        else:
            display = f"{days} days ago"
    elif first_seen_dt:
        delta = now - first_seen_dt
        hours = int(delta.total_seconds() // 3600)
        if hours < 24:
            display = f"{max(0, hours)} hour{'s' if hours != 1 else ''} ago"
        else:
            display = f"{hours // 24} days ago"
    else:
        display = "?"

    parts = []
    if date_posted:
        parts.append(f"Posted: {date_posted}")
    if first_seen_dt:
        parts.append(f"First seen: {first_seen_dt.strftime('%Y-%m-%d %H:%M')}")
    elif first_seen:
        parts.append(f"First seen: {first_seen}")
    tooltip = " · ".join(parts) if parts else "No date available"

    return display, tooltip


def render_page() -> str:
    jobs = get_jobs()
    notes = get_all_role_notes()
    cutoff = (datetime.now() - timedelta(days=RECENT_CUTOFF_DAYS)).strftime("%Y-%m-%d")

    saved = [j for j in jobs if j["status"] == "saved"]
    recent = [j for j in jobs if j["status"] == "new" and (j.get("date_posted") or "") >= cutoff]
    older = [j for j in jobs if j["status"] == "new" and (j.get("date_posted") or "") < cutoff]

    def sort_by_fit(j: dict) -> tuple:
        score = j.get("fit_score")
        # Scored rows first (1 > 0 reversed); then score desc; then date desc
        return (0 if score is None else 1, score or 0, j.get("date_posted") or "")

    recent.sort(key=sort_by_fit, reverse=True)
    older.sort(key=sort_by_fit, reverse=True)

    def job_rows(section: list[dict]) -> str:
        rows = []
        for job in section:
            jid = job["id"]
            date_display, date_tooltip = relative_time(
                job.get("date_posted"), job.get("first_seen")
            )
            company_raw = job.get("company") or ""
            company = escape(company_raw or "?")
            url = escape(job.get("job_url") or "#")
            title = escape(job.get("title") or "Unknown")
            location = escape(job.get("location") or "")
            status = job["status"]

            note_entry = notes.get(jid)
            company_attr = escape(company_raw, quote=True)

            if status == "saved":
                actions = f'<button class="btn btn-reset" onclick="setStatus({jid},\'new\')">unsave</button>'
            else:
                actions = (
                    f'<button class="btn btn-save" onclick="setStatus({jid},\'saved\')">save</button> '
                    f'<button class="btn btn-discard" onclick="setStatus({jid},\'discarded\')">discard</button>'
                )

            source = escape(job.get("source") or "")

            us_flag = (
                ' <span class="us-only" data-tooltip="Posting may require US work authorization">US?</span>'
                if job.get("us_only")
                else ""
            )

            stack_flag = (
                f' <span class="stack-flag" data-tooltip="{escape(FLAG_TOOLTIP, quote=True)}">'
                f"{escape(FLAG_LABEL)}</span>"
                if job.get("azure_only")
                else ""
            )

            has_note = "has-note" if note_entry else ""
            note_tooltip = "View/edit note" if note_entry else "Add note"
            note_icon = (
                f' <span class="note-icon {has_note}"'
                f' data-tooltip="{note_tooltip}" onclick="toggleNote({jid})">📝</span>'
            )
            note_row = (
                f'<tr id="note-row-{jid}" class="note-row" hidden>'
                f'<td colspan="8">'
                f'<div class="note-editor">'
                f'<textarea id="note-text-{jid}" rows="3"'
                f' placeholder="Note about this role..."></textarea>'
                f'<div class="note-actions">'
                f'<button class="btn btn-save" onclick="saveNote({jid})">save</button> '
                f'<button class="btn btn-discard" onclick="discardNote({jid})">discard</button>'
                f"</div></div></td></tr>"
            )
            if company_raw:
                discard_co_btn = (
                    f' <button class="btn-discard-co" data-company="{company_attr}"'
                    f' data-tooltip="Discard this company and all its roles"'
                    f' onclick="discardCompany(this)">✕</button>'
                )
            else:
                discard_co_btn = ""

            fit_score = job.get("fit_score")
            fit_rationale = job.get("fit_rationale") or ""
            if fit_score is None:
                fit_cell = '<td class="fit"></td>'
            else:
                if fit_score >= 70:
                    tier = "fit-high"
                elif fit_score >= 40:
                    tier = "fit-mid"
                else:
                    tier = "fit-low"
                fit_cell = (
                    f'<td class="fit"><span class="fit-badge {tier}"'
                    f' data-tooltip="{escape(fit_rationale, quote=True)}">{fit_score}</span></td>'
                )

            rows.append(
                f'<tr id="job-{jid}">'
                f"<td>{jid}</td>"
                f"{fit_cell}"
                f'<td class="posted" data-tooltip="{escape(date_tooltip, quote=True)}">{escape(date_display)}</td>'
                f'<td><span class="source">{source}</span></td>'
                f'<td><a href="{url}" target="_blank">{title}</a>{us_flag}{stack_flag}{note_icon}</td>'
                f'<td>{company}{discard_co_btn}</td>'
                f"<td>{location}</td>"
                f'<td class="actions">{actions}</td>'
                f"</tr>"
                f"{note_row}"
            )
        return "\n".join(rows)

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(saved) + len(recent) + len(older)

    notes_map = {jid: v["note"] for jid, v in notes.items()}
    notes_json = json.dumps(notes_map).replace("</", "<\\/")

    table_header = (
        "<table><thead><tr>"
        "<th>ID</th><th>Fit</th><th>Posted</th><th>Source</th><th>Title</th><th>Company</th><th>Location</th><th></th>"
        "</tr></thead>\n"
    )

    sections_html = ""

    if saved:
        sections_html += f'<h2>Saved <span class="count">({len(saved)})</span></h2>\n'
        sections_html += table_header
        sections_html += f"<tbody>\n{job_rows(saved)}\n</tbody></table>\n"

    if recent:
        sections_html += f'<h2>Recent <span class="count">({len(recent)} in last {RECENT_CUTOFF_DAYS} days)</span></h2>\n'
        sections_html += table_header
        sections_html += f"<tbody>\n{job_rows(recent)}\n</tbody></table>\n"

    if older:
        sections_html += f'<h2>Older <span class="count">({len(older)})</span></h2>\n'
        sections_html += table_header
        sections_html += f"<tbody>\n{job_rows(older)}\n</tbody></table>\n"

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Results</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .meta {{ color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 2px solid #e0e0e0; padding-bottom: 0.3rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; }}
  th {{ background: #f0f0f0; font-weight: 600; position: sticky; top: 0; }}
  tr:hover {{ background: #f5f5f0; }}
  a {{ color: #1a6dd4; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .count {{ color: #666; font-weight: normal; }}
  td.actions {{ white-space: nowrap; min-width: 9rem; text-align: right; }}
  .btn {{ border: none; padding: 0.25rem 0.5rem; border-radius: 3px; cursor: pointer; font-size: 0.8rem; }}
  .btn-save {{ background: #d4edda; color: #155724; }}
  .btn-save:hover {{ background: #c3e6cb; }}
  .btn-discard {{ background: #f8d7da; color: #721c24; }}
  .btn-discard:hover {{ background: #f1c0c5; }}
  .btn-reset {{ background: #e2e3e5; color: #383d41; }}
  .btn-reset:hover {{ background: #d6d8db; }}
  .source {{ background: #e8eaf6; color: #3949ab; padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.75rem; font-weight: 600; white-space: nowrap; }}
  .us-only {{ background: #fff3cd; color: #856404; padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.75rem; font-weight: 600; margin-left: 0.4rem; position: relative; }}
  .stack-flag {{ background: #cfe2ff; color: #0a4275; padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.75rem; font-weight: 600; margin-left: 0.4rem; position: relative; }}
  .note-icon {{ cursor: pointer; margin-left: 0.4rem; font-size: 0.85rem; position: relative; opacity: 0.25; filter: grayscale(1); }}
  .note-icon.has-note {{ opacity: 1; filter: none; }}
  tr:hover .note-icon {{ opacity: 1; }}
  .note-icon:hover {{ filter: none; }}
  .btn-discard-co {{ border: none; background: transparent; color: #b04050; cursor: pointer; padding: 0 0.25rem; font-size: 0.85rem; line-height: 1; opacity: 0.35; position: relative; }}
  tr:hover .btn-discard-co {{ opacity: 1; }}
  .btn-discard-co:hover {{ color: #721c24; }}
  tr.note-row > td {{ background: #fffbe6; padding: 0.6rem 1rem; border-bottom: 1px solid #eee; }}
  .note-editor textarea {{ width: 100%; box-sizing: border-box; font: inherit; padding: 0.4rem; border: 1px solid #d0d0d0; border-radius: 3px; resize: vertical; }}
  .note-editor .note-actions {{ margin-top: 0.4rem; }}
  tr.fading {{ opacity: 0.3; transition: opacity 0.3s; }}
  td.fit {{ width: 2.5rem; text-align: center; padding: 0.4rem 0.3rem; position: relative; }}
  td.posted {{ white-space: nowrap; position: relative; cursor: help; color: #555; }}
  .fit-badge {{ display: inline-block; min-width: 1.8rem; padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.78rem; font-weight: 600; cursor: help; position: relative; }}
  .fit-high {{ background: #d4edda; color: #155724; }}
  .fit-mid  {{ background: #fff3cd; color: #856404; }}
  .fit-low  {{ background: #f8d7da; color: #721c24; }}
  [data-tooltip]:hover::after {{
    content: attr(data-tooltip);
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
    bottom: calc(100% + 6px);
    background: #2a2a2a;
    color: #f5f5f5;
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    font-size: 0.78rem;
    font-weight: 400;
    line-height: 1.35;
    white-space: normal;
    width: max-content;
    max-width: 360px;
    z-index: 20;
    pointer-events: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.18);
  }}
</style>
</head>
<body>
<h1>Job Search Results</h1>
<p class="meta">Generated {generated} &middot; {total} results</p>
{sections_html}
<script>
const NOTES = {notes_json};

function toggleNote(id) {{
  const row = document.getElementById('note-row-' + id);
  if (!row) return;
  const willOpen = row.hasAttribute('hidden');
  if (willOpen) {{
    const ta = document.getElementById('note-text-' + id);
    ta.value = NOTES[id] || '';
    row.removeAttribute('hidden');
    ta.focus();
  }} else {{
    row.setAttribute('hidden', '');
  }}
}}

async function saveNote(id) {{
  const note = document.getElementById('note-text-' + id).value.trim();
  if (!note) {{ return discardNote(id); }}
  try {{
    const res = await fetch('/api/notes', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{job_id: id, note}})
    }});
    if (res.ok) location.reload();
  }} catch (e) {{ console.error(e); }}
}}

async function discardNote(id) {{
  try {{
    const res = await fetch('/api/notes', {{
      method: 'DELETE',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{job_id: id}})
    }});
    if (res.ok) location.reload();
  }} catch (e) {{ console.error(e); }}
}}

async function discardCompany(btn) {{
  const company = btn.dataset.company;
  if (!company) return;
  if (!confirm('Discard all roles from ' + company + '?\\nFuture postings from this company will be auto-discarded.')) return;
  try {{
    const res = await fetch('/api/companies/discard', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{company}})
    }});
    if (res.ok) location.reload();
  }} catch (e) {{ console.error(e); }}
}}

async function setStatus(id, status) {{
  const row = document.getElementById('job-' + id);
  try {{
    const res = await fetch('/api/status', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id, status}})
    }});
    if (res.ok) {{
      row.classList.add('fading');
      setTimeout(() => location.reload(), 350);
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(port=5000, debug=True)
