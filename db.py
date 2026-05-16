"""SQLite persistence for job search results."""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "jobs.db"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    job_url TEXT UNIQUE NOT NULL,
    date_posted TEXT,
    search_term TEXT,
    description TEXT,
    us_only INTEGER NOT NULL DEFAULT 0,
    azure_only INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_notes (
    company_key TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discarded_companies (
    company_key TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN description TEXT",
    "ALTER TABLE jobs ADD COLUMN us_only INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN source TEXT",
    "ALTER TABLE jobs ADD COLUMN azure_only INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN fit_score INTEGER",
    "ALTER TABLE jobs ADD COLUMN fit_rationale TEXT",
    "ALTER TABLE jobs ADD COLUMN scored_at TEXT",
]

# Backfill source from job_url for rows that predate the source column
SOURCE_BACKFILLS = [
    ("indeed", "%indeed.com%"),
    ("linkedin", "%linkedin.com%"),
    ("google", "%google.com/search%"),
    ("hn", "%news.ycombinator.com%"),
]

US_ONLY_PATTERNS = [
    "authorized to work in the united states",
    "authorization to work in the u.s",
    "authorization to work in the us",
    "must be authorized to work in the u",
    "eligibility to work in the united states",
    "work authorization in the united states",
    "u.s. citizen",
    "us citizen",
    "united states citizenship",
    "must reside in the u",
    "must be located in the u",
    "must be based in the u",
    "remote - us only",
    "remote (us only)",
    "remote (us)",
    "open to us-based candidates only",
    "us (remote)",
    "u.s. (remote)",
    "usa (remote)",
    "(remote, us)",
    "(remote, u.s.)",
    "(remote, usa)",
    "(remote/us)",
    "- us remote",
    "- remote, us",
    "- us (remote)",
    "(us remote)",
    "remote - us",
    "remote - united states",
    "remote, us",
    "remote, usa",
    "remote, united states",
]


def check_us_only(description: str | None, location: str | None, title: str | None = None) -> bool:
    # Check description and title but NOT the generic location field,
    # since Indeed sets location to "Remote, US" for all US remote jobs
    desc_text = ((description or "") + " " + (title or "")).lower()
    return any(p in desc_text for p in US_ONLY_PATTERNS)


from config import FLAG_MUST_MATCH, FLAG_MUST_NOT_MATCH

_FLAG_MUST = [re.compile(p, re.IGNORECASE) for p in FLAG_MUST_MATCH]
_FLAG_MUST_NOT = [re.compile(p, re.IGNORECASE) for p in FLAG_MUST_NOT_MATCH]


def check_flag(description: str | None, title: str | None = None) -> bool:
    """Apply the configured stack-flag patterns.

    True iff every must_match pattern hits AND no must_not_match pattern hits.
    """
    text = (description or "") + " " + (title or "")
    if _FLAG_MUST and not all(r.search(text) for r in _FLAG_MUST):
        return False
    return not any(r.search(text) for r in _FLAG_MUST_NOT)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    azure_added = False
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
            if "azure_only" in migration:
                azure_added = True
        except sqlite3.OperationalError:
            pass  # column already exists
    for source, pattern in SOURCE_BACKFILLS:
        conn.execute(
            "UPDATE jobs SET source = ? WHERE source IS NULL AND job_url LIKE ?",
            (source, pattern),
        )
    conn.execute(
        "UPDATE jobs SET date_posted = substr(first_seen, 1, 10)"
        " WHERE date_posted IS NULL OR date_posted = ''"
    )
    if azure_added:
        rows = conn.execute("SELECT id, description, title FROM jobs").fetchall()
        for row in rows:
            flag = 1 if check_flag(row["description"], row["title"]) else 0
            if flag:
                conn.execute("UPDATE jobs SET azure_only = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    return conn


def upsert_jobs(df: pd.DataFrame) -> tuple[int, int]:
    """Insert new jobs, update last_seen for existing ones. Returns (new, updated)."""
    conn = get_db()
    now = datetime.now().isoformat()
    new_count = 0
    updated_count = 0
    discarded_keys = {
        r[0] for r in conn.execute("SELECT company_key FROM discarded_companies").fetchall()
    }

    for _, row in df.iterrows():
        url = row.get("job_url")
        if not url or pd.isna(url):
            continue

        title = row.get("title")
        if pd.isna(title):
            title = None
        title = clean_title(title)
        company = row.get("company")
        if pd.isna(company):
            company = None
        source = row.get("source")
        if pd.isna(source):
            source = None

        existing = conn.execute(
            "SELECT id, description, source, job_url FROM jobs WHERE job_url = ?", (url,)
        ).fetchone()
        if not existing and title and company and source:
            existing = conn.execute(
                "SELECT id, description, source, job_url FROM jobs"
                " WHERE LOWER(title) = LOWER(?) AND LOWER(company) = LOWER(?) AND source = ?",
                (title, company, source),
            ).fetchone()
        if existing:
            description = row.get("description")
            if pd.isna(description):
                description = None
            location = row.get("location")
            if pd.isna(location):
                location = None

            updates = {"last_seen": now}
            if description and not existing["description"]:
                updates["description"] = description
                updates["us_only"] = check_us_only(description, location, title)
                updates["azure_only"] = check_flag(description, title)

            if source and not existing["source"]:
                updates["source"] = source

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE jobs SET {set_clause} WHERE id = ?",  # noqa: S608
                [*updates.values(), existing["id"]],
            )
            updated_count += 1
        else:
            if pd.notna(row.get("date_posted")):
                date_posted = str(row["date_posted"])[:10]
            else:
                date_posted = now[:10]

            description = row.get("description")
            if pd.isna(description):
                description = None

            location = row.get("location")
            if pd.isna(location):
                location = None

            us_only = check_us_only(description, location, title)
            azure_only = check_flag(description, title)
            status = "discarded" if normalize_company_key(company) in discarded_keys else "new"

            conn.execute(
                "INSERT INTO jobs (title, company, location, job_url, date_posted, search_term,"
                " description, us_only, azure_only, source, status, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    company,
                    location,
                    url,
                    date_posted,
                    row.get("search_term"),
                    description,
                    us_only,
                    azure_only,
                    source,
                    status,
                    now,
                    now,
                ),
            )
            new_count += 1

    conn.commit()
    conn.close()
    return new_count, updated_count


def set_status(ids: list[int], status: str) -> int:
    """Set status for jobs by ID. Returns number of rows affected."""
    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"UPDATE jobs SET status = ? WHERE id IN ({placeholders})",  # noqa: S608
        [status, *ids],
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected


def discard_matching(keywords: list[str], column: str = "title") -> int:
    """Discard existing jobs whose <column> matches any of the given keywords."""
    if column not in ("title", "company"):
        raise ValueError(f"Invalid column: {column}")
    conn = get_db()
    total = 0
    for kw in keywords:
        cursor = conn.execute(
            f"UPDATE jobs SET status = 'discarded' WHERE status = 'new' AND LOWER({column}) LIKE ?",  # noqa: S608
            (f"%{kw.lower()}%",),
        )
        total += cursor.rowcount
    conn.commit()
    conn.close()
    return total


def dedupe_jobs() -> int:
    """Collapse rows sharing (title, company, source). Keep the oldest; discard the rest."""
    conn = get_db()
    groups = conn.execute(
        "SELECT LOWER(title) t, LOWER(company) c, source, COUNT(*) n"
        " FROM jobs WHERE title IS NOT NULL AND company IS NOT NULL AND source IS NOT NULL"
        " GROUP BY t, c, source HAVING n > 1"
    ).fetchall()
    discarded = 0
    for g in groups:
        rows = conn.execute(
            "SELECT id, status, description FROM jobs"
            " WHERE LOWER(title) = ? AND LOWER(company) = ? AND source = ?"
            " ORDER BY first_seen ASC, id ASC",
            (g["t"], g["c"], g["source"]),
        ).fetchall()
        keeper = rows[0]
        dup_ids = [r["id"] for r in rows[1:]]
        # Backfill description onto keeper if it's missing
        if not keeper["description"]:
            for r in rows[1:]:
                if r["description"]:
                    conn.execute(
                        "UPDATE jobs SET description = ? WHERE id = ?",
                        (r["description"], keeper["id"]),
                    )
                    break
        # If keeper was already discarded but a dup had a live status, promote that status
        if keeper["status"] == "discarded":
            for r in rows[1:]:
                if r["status"] not in ("discarded",):
                    conn.execute(
                        "UPDATE jobs SET status = ? WHERE id = ?", (r["status"], keeper["id"])
                    )
                    break
        placeholders = ",".join("?" * len(dup_ids))
        conn.execute(
            f"UPDATE jobs SET status = 'discarded' WHERE id IN ({placeholders})",  # noqa: S608
            dup_ids,
        )
        discarded += len(dup_ids)
    conn.commit()
    conn.close()
    return discarded


def refresh_us_only_flags() -> int:
    """Re-scan all jobs and update us_only flags based on description/location."""
    conn = get_db()
    rows = conn.execute("SELECT id, description, location, title FROM jobs").fetchall()
    updated = 0
    for row in rows:
        flag = check_us_only(row["description"], row["location"], row["title"])
        conn.execute("UPDATE jobs SET us_only = ? WHERE id = ?", (flag, row["id"]))
        if flag:
            updated += 1
    conn.commit()
    conn.close()
    return updated


def refresh_stack_flag() -> int:
    """Re-evaluate the stack flag for every job using the current config patterns."""
    conn = get_db()
    rows = conn.execute("SELECT id, description, title FROM jobs").fetchall()
    flagged = 0
    for row in rows:
        flag = check_flag(row["description"], row["title"])
        conn.execute("UPDATE jobs SET azure_only = ? WHERE id = ?", (flag, row["id"]))
        if flag:
            flagged += 1
    conn.commit()
    conn.close()
    return flagged


_COMPANY_NAME_LSTRIP = re.compile(r"^[^\w]+", re.UNICODE)
_COMPANY_NAME_CUT = re.compile(r"[^\w\s&'\-]", re.UNICODE)

_TITLE_TRAILING_JUNK = re.compile(r"[\s\-–—|/\\(\[{,;:]+$")


def clean_title(title: str | None) -> str | None:
    """Strip trailing whitespace and dangling punctuation from a role title.

    Indeed sometimes returns titles like 'Distinguished Engineer -' where the
    upstream title was truncated. HN headers truncated at 200 chars often end
    in '|', '/', ',' or similar. Closing brackets and sentence punctuation
    (')', ']', '?', '!', '.', '+') are kept since they are usually meaningful.
    """
    if not title:
        return title
    cleaned = _TITLE_TRAILING_JUNK.sub("", title).strip()
    return cleaned or None


def clean_company_name(name: str | None) -> str | None:
    """Trim a company name at the first character outside [\\w\\s&'-].

    Skips any leading punctuation first so '[LiveKit](' becomes 'LiveKit', then
    cuts at the next stray character. Handles HN posts that violate the
    'Company | Location | ...' format and carry URL parens, slashes, em-dashes,
    or whole sentences in the company field.
    """
    if not name:
        return name
    name = _COMPANY_NAME_LSTRIP.sub("", name)
    m = _COMPANY_NAME_CUT.search(name)
    if m:
        name = name[: m.start()]
    name = name.strip()
    return name or None


_COMPANY_KEY_NOISE = re.compile(r"[^a-z0-9]+")
_COMPANY_KEY_SUFFIXES = (
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "corp", "corporation",
    "co", "gmbh", "sa", "ag", "bv", "nv", "pty", "pte", "srl",
)


def normalize_company_key(name: str | None) -> str:
    """Map a company display name to a stable lookup key.

    Lowercases, strips punctuation/whitespace, and drops common legal suffixes
    so 'Acme, Inc.' and 'acme inc' collapse to the same key.
    """
    if not name:
        return ""
    key = _COMPANY_KEY_NOISE.sub("", name.lower())
    while True:
        for suffix in _COMPANY_KEY_SUFFIXES:
            if len(key) > len(suffix) and key.endswith(suffix):
                key = key[: -len(suffix)]
                break
        else:
            break
    return key


def get_note(company: str) -> dict | None:
    key = normalize_company_key(company)
    if not key:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT company_key, company_name, note, created_at, updated_at"
        " FROM company_notes WHERE company_key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_notes() -> dict[str, dict]:
    """Return all notes keyed by normalized company_key."""
    conn = get_db()
    rows = conn.execute(
        "SELECT company_key, company_name, note, created_at, updated_at FROM company_notes"
    ).fetchall()
    conn.close()
    return {r["company_key"]: dict(r) for r in rows}


def upsert_note(company: str, note: str) -> bool:
    key = normalize_company_key(company)
    if not key:
        return False
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO company_notes (company_key, company_name, note, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(company_key) DO UPDATE SET"
        "   note = excluded.note,"
        "   company_name = excluded.company_name,"
        "   updated_at = excluded.updated_at",
        (key, company, note, now, now),
    )
    conn.commit()
    conn.close()
    return True


def discard_company(company: str) -> tuple[bool, int]:
    """Mark a company as discarded and discard its existing non-discarded jobs.

    Returns (added, affected_jobs). `added` is False if the company was already
    in the discard list. Existing jobs are matched by normalized company_key,
    so 'Acme, Inc.' catches rows stored as 'Acme'.
    """
    key = normalize_company_key(company)
    if not key:
        return False, 0
    now = datetime.now().isoformat()
    conn = get_db()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO discarded_companies (company_key, company_name, created_at)"
        " VALUES (?, ?, ?)",
        (key, company, now),
    )
    added = cursor.rowcount > 0

    affected = 0
    rows = conn.execute(
        "SELECT id, company FROM jobs WHERE status != 'discarded' AND company IS NOT NULL"
    ).fetchall()
    matching = [r["id"] for r in rows if normalize_company_key(r["company"]) == key]
    if matching:
        placeholders = ",".join("?" * len(matching))
        cursor = conn.execute(
            f"UPDATE jobs SET status = 'discarded' WHERE id IN ({placeholders})",  # noqa: S608
            matching,
        )
        affected = cursor.rowcount
    conn.commit()
    conn.close()
    return added, affected


def get_discarded_company_keys() -> set[str]:
    conn = get_db()
    rows = conn.execute("SELECT company_key FROM discarded_companies").fetchall()
    conn.close()
    return {r["company_key"] for r in rows}


def delete_note(company: str) -> bool:
    key = normalize_company_key(company)
    if not key:
        return False
    conn = get_db()
    cursor = conn.execute("DELETE FROM company_notes WHERE company_key = ?", (key,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


_URL_LINE = re.compile(r"^https?://", re.IGNORECASE)


def reconstruct_hn_header(description: str | None) -> str | None:
    """Re-join a HN comment's first line that was split by old strip_html.

    Old strip_html replaced every HTML tag with '\\n', so '<a>URL</a>' wrappers
    fragmented the header. We rejoin lines while brackets are unbalanced, the
    next line is a URL, or the next line starts with continuation punctuation.
    """
    if not description:
        return None
    lines = [line.strip() for line in description.split("\n") if line.strip()]
    if not lines:
        return None
    result = lines[0]
    for line in lines[1:]:
        opens = (
            result.count("(") - result.count(")") + result.count("[") - result.count("]")
        )
        is_url = bool(_URL_LINE.match(line))
        starts_continuation = line[:1] in ")]|,;"
        if opens > 0 or is_url or starts_continuation:
            if result.endswith(("(", "[")) or line.startswith((")", "]")):
                result += line
            else:
                result += " " + line
        else:
            break
    return re.sub(r"\s+", " ", result).strip()[:200]


def backfill_hn_titles() -> tuple[int, list[tuple[int, str, str]]]:
    """Rebuild titles for HN rows where the title looks truncated.

    Returns (count_changed, sample_changes).
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, description FROM jobs WHERE source = 'hn' AND description IS NOT NULL"
    ).fetchall()
    changes = []
    for r in rows:
        title = (r["title"] or "").rstrip()
        # Titles truncated by the old strip_html bug end with continuation
        # punctuation: an open bracket awaiting a URL, or a pipe awaiting the
        # next header segment.
        if not title.endswith(("(", "[", "|", "](")):
            continue
        new_title = reconstruct_hn_header(r["description"])
        if new_title and new_title != r["title"]:
            changes.append((r["id"], r["title"], new_title))
    for jid, _, new_title in changes:
        conn.execute("UPDATE jobs SET title = ? WHERE id = ?", (new_title, jid))
    conn.commit()
    conn.close()
    return len(changes), changes


def clean_hn_company_names() -> int:
    """Apply clean_company_name to existing HN rows. Returns rows changed."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, company FROM jobs WHERE company IS NOT NULL AND source = 'hn'"
    ).fetchall()
    changed = 0
    for r in rows:
        cleaned = clean_company_name(r["company"])
        if cleaned != r["company"]:
            conn.execute("UPDATE jobs SET company = ? WHERE id = ?", (cleaned, r["id"]))
            changed += 1
    conn.commit()
    conn.close()
    return changed


def get_jobs(include_discarded: bool = False) -> list[dict]:
    """Get jobs, excluding discarded by default."""
    conn = get_db()
    if include_discarded:
        rows = conn.execute("SELECT * FROM jobs ORDER BY date_posted DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status != 'discarded' ORDER BY date_posted DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unscored_jobs(
    min_description_len: int = 200, statuses: tuple[str, ...] = ("new",)
) -> list[dict]:
    """Jobs needing a fit_score: status in `statuses`, has description, not yet scored."""
    conn = get_db()
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT id, status, title, company, location, description, source FROM jobs"
        f" WHERE status IN ({placeholders}) AND fit_score IS NULL"
        f" AND description IS NOT NULL AND length(description) > ?"
        f" ORDER BY COALESCE(date_posted, '') DESC, id DESC",
        (*statuses, min_description_len),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_scoring_examples(
    discard_limit: int = 50, min_description_len: int = 200
) -> tuple[list[dict], list[dict]]:
    """Return (saved, discarded) example rows with descriptions for prompting."""
    conn = get_db()
    saves = conn.execute(
        "SELECT id, title, company, location, description FROM jobs"
        " WHERE status = 'saved'"
        " AND description IS NOT NULL AND length(description) > ?"
        " ORDER BY id DESC",
        (min_description_len,),
    ).fetchall()
    discards = conn.execute(
        "SELECT id, title, company, location, description FROM jobs"
        " WHERE status = 'discarded'"
        " AND description IS NOT NULL AND length(description) > ?"
        " ORDER BY id DESC LIMIT ?",
        (min_description_len, discard_limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in saves], [dict(r) for r in discards]


def auto_discard_low_scores(threshold: int) -> int:
    """Discard 'new' rows with fit_score below `threshold`. Saves are protected.

    Rows with fit_score IS NULL are left alone (NULL comparisons return NULL,
    so they don't satisfy the predicate).
    """
    if threshold <= 0:
        return 0
    conn = get_db()
    cursor = conn.execute(
        "UPDATE jobs SET status = 'discarded'"
        " WHERE status = 'new' AND fit_score < ?",
        (threshold,),
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected


def set_score(job_id: int, score: int, rationale: str) -> None:
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE jobs SET fit_score = ?, fit_rationale = ?, scored_at = ? WHERE id = ?",
        (score, rationale, now, job_id),
    )
    conn.commit()
    conn.close()
