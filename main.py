#!/usr/bin/env python3
"""Search for remote SRE/Platform/DevOps/Senior SWE roles using JobSpy."""

import sys

import pandas as pd
from jobspy import scrape_jobs

from config import (
    EXCLUDE_COMPANIES,
    EXCLUDE_TITLE_KEYWORDS,
    LINKEDIN_FETCH_DESCRIPTION,
    REGIONS,
    RESULTS_PER_QUERY,
    SEARCH_QUERIES,
    SITES,
)
from db import dedupe_jobs, discard_matching, get_jobs, set_status, upsert_jobs
from hn import scrape_hn

_EXCLUDE_PATTERN = "|".join(EXCLUDE_TITLE_KEYWORDS)
_EXCLUDE_COMPANY_PATTERN = "|".join(EXCLUDE_COMPANIES)


def filter_titles(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose title matches any excluded keyword."""
    if df.empty or "title" not in df.columns or not _EXCLUDE_PATTERN:
        return df
    mask = ~df["title"].str.contains(_EXCLUDE_PATTERN, case=False, na=False)
    return df[mask]


def filter_companies(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose company matches any excluded name."""
    if df.empty or "company" not in df.columns or not _EXCLUDE_COMPANY_PATTERN:
        return df
    mask = ~df["company"].str.contains(_EXCLUDE_COMPANY_PATTERN, case=False, na=False)
    return df[mask]


def scrape() -> pd.DataFrame:
    all_jobs = []

    for country, location, label in REGIONS:
        for term in SEARCH_QUERIES:
            print(f"[{label}] Searching: {term} ...")
            try:
                jobs = scrape_jobs(
                    site_name=SITES,
                    search_term=term,
                    location=location,
                    is_remote=True,
                    results_wanted=RESULTS_PER_QUERY,
                    country_indeed=country,
                    linkedin_fetch_description=LINKEDIN_FETCH_DESCRIPTION,
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
                )
                if not jobs.empty:
                    jobs["search_term"] = term
                    all_jobs.append(jobs)
                    print(f"  Found {len(jobs)} results")
                else:
                    print("  No results")
            except Exception as e:
                print(f"  Error: {e}")

    if not all_jobs:
        print("\nNo results found.")
        return pd.DataFrame()

    all_jobs = [j.dropna(axis=1, how="all") for j in all_jobs if not j.empty]
    if not all_jobs:
        print("\nNo results found.")
        return pd.DataFrame()
    df = pd.concat(all_jobs, ignore_index=True)
    df = df.drop_duplicates(subset=["job_url"], keep="first")

    df = filter_titles(df)
    df = filter_companies(df)

    # Map jobspy's 'site' column to our 'source' column
    if "site" in df.columns:
        df["source"] = df["site"].astype(str)
    else:
        df["source"] = None

    cols = [
        "title", "company", "location", "job_url", "date_posted",
        "search_term", "description", "source",
    ]
    available_cols = [c for c in cols if c in df.columns]
    df = df[available_cols]

    if "date_posted" in df.columns:
        df["date_posted"] = pd.to_datetime(df["date_posted"], errors="coerce")

    return df


def cmd_search():
    df = scrape()
    if not df.empty:
        new, updated = upsert_jobs(df)
        print(f"\n{new} new jobs added, {updated} existing jobs seen again.")

    # Also scrape HN "Who is hiring?"
    hn_df = filter_companies(filter_titles(scrape_hn()))
    if not hn_df.empty:
        hn_new, hn_updated = upsert_jobs(hn_df)
        print(f"[HN] {hn_new} new jobs added, {hn_updated} existing jobs seen again.")

    # Discard any existing jobs matching new exclusions
    discarded = discard_matching(EXCLUDE_TITLE_KEYWORDS)
    if discarded:
        print(f"Discarded {discarded} existing job(s) matching title filters.")
    discarded_co = discard_matching(EXCLUDE_COMPANIES, column="company")
    if discarded_co:
        print(f"Discarded {discarded_co} existing job(s) matching company filters.")

    # Collapse duplicates sharing (title, company, source)
    dupes = dedupe_jobs()
    if dupes:
        print(f"Discarded {dupes} duplicate row(s).")


def cmd_serve():
    from server import app

    jobs = get_jobs()
    saved = sum(1 for j in jobs if j["status"] == "saved")
    new = sum(1 for j in jobs if j["status"] == "new")
    print(f"Database: {new} new, {saved} saved")
    app.run(port=5000, debug=True)


def cmd_set_status(status: str, ids: list[str]):
    try:
        int_ids = [int(i) for i in ids]
    except ValueError:
        print("Error: IDs must be integers.")
        sys.exit(1)

    affected = set_status(int_ids, status)
    print(f"Marked {affected} job(s) as '{status}'.")


def cmd_hn():
    hn_df = filter_companies(filter_titles(scrape_hn()))
    if hn_df.empty:
        return
    new, updated = upsert_jobs(hn_df)
    print(f"[HN] {new} new jobs added, {updated} existing jobs seen again.")


def cmd_score(limit: int | None = None):
    from score import cmd_score as run_score

    run_score(limit=limit)


def cmd_backfill(limit: int | None = None):
    from backfill import cmd_backfill_linkedin_descriptions

    cmd_backfill_linkedin_descriptions(limit=limit)


def cmd_recompute_flags():
    from db import refresh_stack_flag

    flagged = refresh_stack_flag()
    print(f"Refreshed stack flag on all rows; {flagged} now flagged.")


USAGE = """\
Usage: main.py <command> [args...]

Commands:
  search            Scrape new jobs from all sources (job boards + HN)
  hn                Scrape only HN 'Who is hiring?' threads
  serve             Start web UI for browsing/triaging jobs
  score [N]         Score unscored 'new' jobs for fit (optional limit N)
  backfill [N]      Backfill missing LinkedIn descriptions (optional limit N)
  recompute-flags   Re-evaluate the stack-flag column for all rows (after editing config)
  save <id> ...     Mark jobs as saved
  discard <id> ...  Mark jobs as discarded
  reset <id> ...    Reset jobs back to 'new'
"""


def main():
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        sys.exit(1)
    elif args[0] == "search":
        cmd_search()
    elif args[0] == "hn":
        cmd_hn()
    elif args[0] == "serve":
        cmd_serve()
    elif args[0] == "score":
        limit = None
        if len(args) > 1:
            try:
                limit = int(args[1])
            except ValueError:
                print(f"Usage: main.py score [N]")
                sys.exit(1)
        cmd_score(limit=limit)
    elif args[0] == "backfill":
        limit = None
        if len(args) > 1:
            try:
                limit = int(args[1])
            except ValueError:
                print(f"Usage: main.py backfill [N]")
                sys.exit(1)
        cmd_backfill(limit=limit)
    elif args[0] == "recompute-flags":
        cmd_recompute_flags()
    elif args[0] in ("save", "discard", "reset"):
        if len(args) < 2:
            print(f"Usage: main.py {args[0]} <id> [id...]")
            sys.exit(1)
        status = "new" if args[0] == "reset" else args[0]
        status = "saved" if status == "save" else status
        cmd_set_status(status, args[1:])
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
