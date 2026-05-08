"""Score 'new' jobs for fit using Claude, weighted against CV + saved/discarded examples."""

import os
from pathlib import Path

import anthropic

from config import (
    CANDIDATE_ELIGIBLE_COUNTRIES,
    CANDIDATE_LOCATION,
    CANDIDATE_NOTES,
    SCORING_DESC_TRUNCATE,
    SCORING_DISCARD_EXAMPLE_LIMIT,
    SCORING_MAX_TOKENS,
    SCORING_MIN_DESCRIPTION_LENGTH,
    SCORING_MODEL,
)
from db import get_scoring_examples, get_unscored_jobs, set_score

DEFAULT_CV_PATH = Path(__file__).parent / "cv.md"


def _resolve_cv_path() -> Path:
    env = os.environ.get("JOB_SEARCH_CV_PATH")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CV_PATH

SYSTEM_PROMPT = """You score job postings for a candidate based on fit (0-100).

Use the SAVED roles as positive signal — these are roles the candidate liked.
Use the DISCARDED roles as negative signal, but with caution: rejection reasons
vary (wrong stack, wrong location, wrong seniority, gut feel), so a posting
that resembles discards is not necessarily a poor fit.

Weight saved roles MORE HEAVILY than discarded roles. Saved examples are scarce
and high-signal; discards are noisy.

Focus on actual role fit (responsibilities, seniority, tech stack, location,
work-auth requirements, remote eligibility). Do not over-weight surface
vocabulary or buzzwords.

LOCATION HARD CAP — apply BEFORE other scoring:
The CANDIDATE CONTEXT block names the candidate's location and the countries
they have work authorization in. If the posting:
  - requires physical presence in a country outside that list, OR
  - is "remote within <country>" / "remote, <country> only" / "remote, <region>
    only" where the country/region excludes the candidate, OR
  - requires work authorization the candidate cannot satisfy (visa,
    citizenship, security clearance tied to a specific country), OR
  - mandates a timezone overlap the candidate's notes rule out,
then score 0-15 regardless of stack/seniority match. Cite the restriction in
the rationale. When the posting is genuinely silent on geography, do NOT
apply the cap — treat it as ordinary fit. Ambiguity is not a restriction.

Score guidance (after the location cap):
- 80-100: strong fit, candidate would likely apply
- 50-79:  plausible fit, worth a closer look
- 20-49:  weak fit, probably not interested
- 0-19:   clear mismatch (includes location-restricted roles)

Output via the score_fit tool only."""

SCORE_TOOL = {
    "name": "score_fit",
    "description": "Record a fit score and short rationale for the job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Fit score from 0 (clear mismatch) to 100 (strong fit).",
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence (≤160 chars) explaining the score.",
            },
        },
        "required": ["score", "rationale"],
        "additionalProperties": False,
    },
}


def _format_example(j: dict) -> str:
    desc = (j.get("description") or "")[:SCORING_DESC_TRUNCATE]
    return (
        f"- {j.get('title') or '?'} @ {j.get('company') or '?'}"
        f" | {j.get('location') or ''}\n  {desc}"
    )


def _build_candidate_context() -> str:
    eligible = ", ".join(CANDIDATE_ELIGIBLE_COUNTRIES) or "(none specified)"
    notes = CANDIDATE_NOTES or "(none)"
    return (
        f"Location: {CANDIDATE_LOCATION}\n"
        f"Work authorization (eligible countries): {eligible}\n"
        f"Notes:\n{notes}"
    )


def _build_cached_block(cv: str, saves: list[dict], discards: list[dict]) -> str:
    saved_block = "\n".join(_format_example(j) for j in saves) or "(none)"
    discard_block = "\n".join(_format_example(j) for j in discards) or "(none)"
    return (
        "=== CANDIDATE CONTEXT ===\n"
        f"{_build_candidate_context()}\n\n"
        "=== CANDIDATE CV (LaTeX source) ===\n"
        f"{cv}\n\n"
        f"=== SAVED ROLES (positive — weight heavily, count={len(saves)}) ===\n"
        f"{saved_block}\n\n"
        f"=== DISCARDED ROLES (negative — noisy signal, count={len(discards)}) ===\n"
        f"{discard_block}"
    )


def _format_target(j: dict) -> str:
    return (
        "Score this posting:\n"
        f"Title: {j.get('title') or '?'}\n"
        f"Company: {j.get('company') or '?'}\n"
        f"Location: {j.get('location') or ''}\n"
        f"Source: {j.get('source') or ''}\n\n"
        f"Description:\n{j.get('description') or ''}"
    )


def _extract_score(response) -> tuple[int, str] | None:
    for block in response.content:
        if block.type == "tool_use" and block.name == "score_fit":
            return int(block.input["score"]), str(block.input["rationale"])
    return None


def cmd_score(limit: int | None = None) -> None:
    api_key = os.environ.get("JOB_SEARCH_API_KEY")
    if not api_key:
        print("Error: JOB_SEARCH_API_KEY environment variable not set.")
        return

    cv_path = _resolve_cv_path()
    if not cv_path.exists():
        print(
            f"Error: CV not found at {cv_path}.\n"
            "Set JOB_SEARCH_CV_PATH to point to your CV file"
            " (plain text, Markdown, or LaTeX), or place it at ./cv.md."
        )
        return

    cv = cv_path.read_text()
    saves, discards = get_scoring_examples(
        discard_limit=SCORING_DISCARD_EXAMPLE_LIMIT,
        min_description_len=SCORING_MIN_DESCRIPTION_LENGTH,
    )
    saves_by_id = {s["id"]: s for s in saves}
    default_block = _build_cached_block(cv, saves, discards)
    print(
        f"Loaded CV ({len(cv)} chars), {len(saves)} saved examples,"
        f" {len(discards)} discarded examples."
    )

    jobs = get_unscored_jobs(
        statuses=("new", "saved"),
        min_description_len=SCORING_MIN_DESCRIPTION_LENGTH,
    )
    if limit is not None:
        jobs = jobs[:limit]
    if not jobs:
        print("No jobs to score.")
        return
    print(f"Scoring {len(jobs)} job(s) with {SCORING_MODEL}...")

    client = anthropic.Anthropic(api_key=api_key)
    cache_hits = 0
    total_in = 0
    total_out = 0
    failures = 0

    for i, job in enumerate(jobs, 1):
        # Exclude the target row from saved examples when scoring it (avoid self-leak).
        if job["id"] in saves_by_id:
            filtered_saves = [s for s in saves if s["id"] != job["id"]]
            cached_block = _build_cached_block(cv, filtered_saves, discards)
        else:
            cached_block = default_block

        try:
            response = client.messages.create(
                model=SCORING_MODEL,
                max_tokens=SCORING_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[SCORE_TOOL],
                tool_choice={"type": "tool", "name": "score_fit"},
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": cached_block,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": _format_target(job)},
                    ],
                }],
            )
        except anthropic.APIError as e:
            failures += 1
            print(f"  [{i}/{len(jobs)}] id={job['id']} API error: {e}")
            continue

        result = _extract_score(response)
        if result is None:
            failures += 1
            print(f"  [{i}/{len(jobs)}] id={job['id']} no score returned")
            continue

        score, rationale = result
        set_score(job["id"], score, rationale)

        usage = response.usage
        total_in += usage.input_tokens
        total_out += usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        if cache_read > 0:
            cache_hits += 1

        title = (job.get("title") or "")[:60]
        print(f"  [{i}/{len(jobs)}] id={job['id']} score={score:3d} {title}")

    print(
        f"\nDone: {len(jobs) - failures} scored, {failures} failed."
        f" Tokens: in={total_in} out={total_out}, cache hits: {cache_hits}/{len(jobs)}."
    )
