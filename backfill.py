"""Targeted backfill: fetch missing LinkedIn descriptions for existing rows."""

import time
from datetime import datetime

from jobspy.linkedin import LinkedIn
from jobspy.model import Country, DescriptionFormat, ScraperInput, Site

from db import check_flag, check_us_only, get_db

REQUEST_DELAY_S = 2.0


def cmd_backfill_linkedin_descriptions(limit: int | None = None) -> None:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, job_url, title, location FROM jobs"
        " WHERE description IS NULL AND source = 'linkedin'"
        " AND job_url LIKE '%/jobs/view/%'"
        " ORDER BY id DESC"
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        print("No LinkedIn rows need description backfill.")
        conn.close()
        return

    print(f"Backfilling descriptions for {len(rows)} LinkedIn row(s)...")

    scraper = LinkedIn(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        )
    )
    scraper.scraper_input = ScraperInput(
        site_type=[Site.LINKEDIN],
        results_wanted=1,
        country=Country.WORLDWIDE,
        description_format=DescriptionFormat.MARKDOWN,
    )

    backfilled = 0
    empty = 0
    errors = 0
    now = datetime.now().isoformat()

    for i, row in enumerate(rows, 1):
        job_id = row["job_url"].rsplit("/", 1)[-1].split("?")[0]
        title_short = (row["title"] or "")[:60]

        try:
            details = scraper._get_job_details(job_id)
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(rows)}] id={row['id']} error: {e}")
            time.sleep(REQUEST_DELAY_S)
            continue

        description = details.get("description")
        if not description:
            empty += 1
            print(f"  [{i}/{len(rows)}] id={row['id']} no description returned: {title_short}")
            time.sleep(REQUEST_DELAY_S)
            continue

        us_only = check_us_only(description, row["location"], row["title"])
        azure_only = check_flag(description, row["title"])
        conn.execute(
            "UPDATE jobs SET description = ?, us_only = ?, azure_only = ?, last_seen = ?"
            " WHERE id = ?",
            (description, us_only, azure_only, now, row["id"]),
        )
        conn.commit()
        backfilled += 1
        print(f"  [{i}/{len(rows)}] id={row['id']} ok ({len(description)} chars): {title_short}")
        time.sleep(REQUEST_DELAY_S)

    conn.close()
    print(
        f"\nDone: {backfilled} backfilled, {empty} returned no description,"
        f" {errors} errored."
    )
