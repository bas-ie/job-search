"""Load personalization settings from config.toml.

Falls back to config.toml.example with a warning so a fresh clone runs without
forcing a copy step. Users should `cp config.toml.example config.toml` and edit.
"""

import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
_CONFIG_PATH = PROJECT_ROOT / "config.toml"
_EXAMPLE_PATH = PROJECT_ROOT / "config.toml.example"


def _load() -> dict:
    if _CONFIG_PATH.exists():
        path = _CONFIG_PATH
    elif _EXAMPLE_PATH.exists():
        print(
            f"Warning: {_CONFIG_PATH.name} not found; using {_EXAMPLE_PATH.name}."
            f" Copy and edit it: cp {_EXAMPLE_PATH.name} {_CONFIG_PATH.name}",
            file=sys.stderr,
        )
        path = _EXAMPLE_PATH
    else:
        raise FileNotFoundError(
            f"Neither {_CONFIG_PATH} nor {_EXAMPLE_PATH} exists."
        )
    with path.open("rb") as f:
        return tomllib.load(f)


_cfg = _load()

SEARCH_QUERIES: list[str] = _cfg["search"]["queries"]
SITES: list[str] = _cfg["search"]["sites"]
REGIONS: list[tuple[str, str, str]] = [
    (r["country_indeed"], r["location"], r["label"]) for r in _cfg["search"]["regions"]
]
RESULTS_PER_QUERY: int = _cfg["search"]["results_per_query"]
LINKEDIN_FETCH_DESCRIPTION: bool = _cfg["search"]["linkedin_fetch_description"]

EXCLUDE_TITLE_KEYWORDS: list[str] = _cfg["filters"]["exclude_title_keywords"]
EXCLUDE_COMPANIES: list[str] = _cfg["filters"]["exclude_companies"]

SCORING_MODEL: str = _cfg["scoring"]["model"]
SCORING_DESC_TRUNCATE: int = _cfg["scoring"]["description_truncate"]
SCORING_MAX_TOKENS: int = _cfg["scoring"]["max_tokens"]
SCORING_DISCARD_EXAMPLE_LIMIT: int = _cfg["scoring"]["discard_example_limit"]
SCORING_MIN_DESCRIPTION_LENGTH: int = _cfg["scoring"]["min_description_length"]

RECENT_CUTOFF_DAYS: int = _cfg["ui"]["recent_cutoff_days"]

FLAG_LABEL: str = _cfg["flag"]["label"]
FLAG_TOOLTIP: str = _cfg["flag"]["tooltip"]
FLAG_MUST_MATCH: list[str] = _cfg["flag"]["must_match"]
FLAG_MUST_NOT_MATCH: list[str] = _cfg["flag"]["must_not_match"]
