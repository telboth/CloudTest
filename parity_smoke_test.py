from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.bug import Bug  # noqa: E402
from Cloud_test import unified_app as app  # noqa: E402


def _make_bug(
    bug_id: int,
    title: str,
    description: str,
    *,
    status: str = "open",
    severity: str = "medium",
    created_days_ago: int = 1,
    updated_days_ago: int = 1,
) -> Bug:
    now = datetime.now(timezone.utc)
    bug = Bug(
        id=bug_id,
        title=title,
        description=description,
        status=status,
        severity=severity,
        reporter_id="reporter@example.com",
        assignee_id="assignee@example.com",
        category="software",
        created_at=now - timedelta(days=created_days_ago),
        updated_at=now - timedelta(days=updated_days_ago),
    )
    return bug


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run() -> None:
    # Sentiment normalization and symbols
    _assert(app._normalize_sentiment_label("Positiv") == "positive", "Sentiment mapping for 'Positiv' failed")
    _assert(app._normalize_sentiment_label("negativ") == "negative", "Sentiment mapping for 'negativ' failed")
    _assert(app._normalize_sentiment_label("ukjent") == "unknown", "Sentiment default mapping failed")
    _assert(app._normalize_sentiment_label("") == "unknown", "Sentiment empty mapping failed")
    _assert(app._sentiment_symbol("positive") == ":-)", "Positive sentiment symbol mismatch")
    _assert(app._sentiment_symbol("neutral") == ":-|", "Neutral sentiment symbol mismatch")
    _assert(app._sentiment_symbol("negative") == ":-(", "Negative sentiment symbol mismatch")
    _assert(app._sentiment_symbol("") == "", "Unknown sentiment symbol should be empty")

    # Email parsing/validation
    emails = app._parse_email_list("A@Example.com, b@example.com; a@example.com")
    _assert(emails == ["a@example.com", "b@example.com"], "Email dedupe/normalization failed")
    _assert(app._is_valid_email("user@example.com"), "Valid email failed validation")
    _assert(not app._is_valid_email("user@invalid"), "Invalid email passed validation")

    # JSON extraction robustness
    payload = app._extract_json_object("Result: {\"title\": \"Hei\", \"severity\": \"high\"}")
    _assert(isinstance(payload, dict) and payload.get("title") == "Hei", "JSON extraction failed for wrapped text")

    # Duplicate detection
    b1 = _make_bug(1, "Kart lagger ved zoom", "Kartet hakker og fryser ved rask zoom i prosjektvisning.")
    b2 = _make_bug(2, "Kart lagger ved zooming", "Kartet hakker og fryser ved rask zoom i prosjektvisning.")
    b3 = _make_bug(3, "Innlogging feiler", "Bruker blir kastet ut ved innlogging.")
    dupes = app._detect_duplicate_bug_pairs([b1, b2, b3], threshold=0.6, limit=10)
    _assert(any(item["delete_bug_id"] in {1, 2} and item["keep_bug_id"] in {1, 2} for item in dupes), "Duplicate detection missed similar bugs")

    # Admin date filter parser
    parsed = app._parse_admin_created_from("2026-04-10")
    _assert(parsed is not None, "Admin date parser failed for valid date")
    _assert(app._parse_admin_created_from("10-04-2026") is None, "Admin date parser accepted invalid date")

    # Stale and critical aging logic
    stale_bug = _make_bug(4, "Gammel sak", "Har ikke blitt oppdatert.", updated_days_ago=9, status="open")
    critical_aging_bug = _make_bug(5, "Kritisk sak", "Kritisk og gammel.", created_days_ago=3, severity="critical")
    _assert(app._is_stale_bug(stale_bug), "Stale bug detection failed")
    _assert(app._is_critical_aging_bug(critical_aging_bug), "Critical aging bug detection failed")

    print("Cloud_test parity smoke test: OK")


if __name__ == "__main__":
    run()
