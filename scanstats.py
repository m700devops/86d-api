"""The scanner's report card: how each AI model reads bottles, from the scan log.

Pure — scan rows in, a summary out — so every rule is tested without a database
(test_scanstats.py). crm.py's GET /v1/crm/scanner feeds it scan_events joined to
scan_outcomes, and the CRM page's Scanner tab draws it.

Both AIs read every photo (main._run_providers), so the two are compared on the
SAME photos, which is the only fair comparison there is:

- A reply that couldn't read the label (unreadable / label_unsupported) is a
  miss someone paid for with a retake, or would have had the other model not
  read it.
- When the two read DIFFERENT bottles before the reply went out (path "both"),
  the app flags the row and the bartender settles it: "this row is right"
  (confirmed) or removing the row (removed). A
  confirmed dispute is a win for the model whose reading was shown and a loss
  for the other. A removed one is a loss for the shown model and says nothing
  about the other — both may have been wrong. When the other model only
  answered after the reply (paths "fast" and "window"), nobody was shown a flag,
  so a disagreement there is only judged by whether the row got removed.
- A shown answer whose row was later removed counts against it. Rows are also
  removed for duplicates and mis-taps, so this over-counts mistakes a little,
  the same for every model.
- Speed is each model's own reply time, whether or not its answer was used.

A row nobody touched is not evidence either way: a finished count doesn't mean
anyone looked, so it is neither a win nor a loss.
"""
import json
import math
import statistics
from typing import Optional

READ_OK = "ok"
COULD_NOT_READ = ("unreadable", "label_unsupported")
ANSWERED = (READ_OK, "no_bottle") + COULD_NOT_READ

# Settled disputes needed before the page will say one model is more accurate.
# Below this the counts are shown but called what they are: too few.
SETTLED_ENOUGH = 20

# With two providers, the one that isn't the answer's is the other one.
OTHER_PROVIDER = {"openai": "gemini", "gemini": "openai"}


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value >= 0 and not math.isnan(value) else None


def _median(values: list) -> Optional[int]:
    return int(statistics.median(values)) if values else None


def _p90(values: list) -> Optional[int]:
    """Nearest rank: the slowest reply in the fastest 90%."""
    if not values:
        return None
    ordered = sorted(values)
    return int(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)])


def _second(row: dict) -> Optional[dict]:
    """The other provider's reading (main._Answer.summary), or None. The column is
    JSON written by this server, but a bad row must never break the report."""
    raw = row.get("second_answer")
    if not raw:
        return None
    try:
        other = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return other if isinstance(other, dict) and other.get("provider") else None


class _Models:
    def __init__(self):
        self.by_key = {}

    def get(self, provider, model) -> dict:
        key = (provider or "?", model or "?")
        entry = self.by_key.get(key)
        if entry is None:
            entry = self.by_key[key] = {
                "provider": key[0], "model": key[1],
                "answers": 0, "unreadable": 0, "shown": 0, "removed": 0,
                "disputes_won": 0, "disputes_lost": 0,
                "_ms": [], "_out": [],
            }
        return entry

    def reply(self, provider, model, status, ms, output_tokens) -> Optional[dict]:
        """Count one reply the model gave; None when it gave none."""
        if status not in ANSWERED:
            return None
        entry = self.get(provider, model)
        entry["answers"] += 1
        if status in COULD_NOT_READ:
            entry["unreadable"] += 1
        if _num(ms) is not None:
            entry["_ms"].append(ms)
        if _num(output_tokens) is not None:
            entry["_out"].append(output_tokens)
        return entry

    def rows(self) -> list:
        out = []
        for entry in self.by_key.values():
            ms, tokens = entry.pop("_ms"), entry.pop("_out")
            entry["median_ms"] = _median(ms)
            entry["p90_ms"] = _p90(ms)
            entry["avg_output_tokens"] = round(sum(tokens) / len(tokens), 1) if tokens else None
            out.append(entry)
        return sorted(out, key=lambda e: (-e["answers"], e["provider"], e["model"]))


def summarize(rows: list, days: int) -> dict:
    """The report for `rows` (scan_events + scan_outcomes.outcome), newest first
    or in any order. See the module docstring for what counts as what."""
    overall = {
        "scans": 0, "identified": 0, "retakes": 0, "no_bottle": 0, "failed": 0,
        "removed": 0, "fast": 0, "late_contradicted": 0, "late_contradicted_removed": 0,
        "flagged": 0, "flagged_confirmed": 0, "flagged_removed": 0,
        "wait_median_ms": None, "wait_p90_ms": None,
    }
    models = _Models()
    failures: dict = {}
    waits = []

    def failed(provider, label):
        if provider:
            per = failures.setdefault(provider, {})
            per[label or "failed"] = per.get(label or "failed", 0) + 1

    for row in rows:
        status = row.get("status") or "error"
        outcome = row.get("outcome")
        opinion = row.get("second_opinion") or ""
        path = row.get("path")
        overall["scans"] += 1
        if _num(row.get("total_ms")) is not None:
            waits.append(row["total_ms"])

        if status == READ_OK:
            overall["identified"] += 1
        elif status in COULD_NOT_READ:
            overall["retakes"] += 1
        elif status == "no_bottle":
            overall["no_bottle"] += 1
        else:
            overall["failed"] += 1

        shown = models.reply(row.get("provider"), row.get("model"), status,
                             row.get("provider_ms"), row.get("output_tokens"))
        other_answer = _second(row)
        other = None
        if other_answer:
            other = models.reply(other_answer.get("provider"), other_answer.get("model"),
                                 other_answer.get("status"), other_answer.get("provider_ms"),
                                 other_answer.get("output_tokens"))

        if status == READ_OK and shown is not None:
            shown["shown"] += 1
            if outcome == "removed":
                shown["removed"] += 1
                overall["removed"] += 1

        if path == "fast":
            overall["fast"] += 1
        if opinion == "disagree" and status == READ_OK and shown is not None:
            if path in ("fast", "window"):
                # Replied before the other model finished (at once from the
                # bar's own book, or after the wait window), and it disagreed
                # afterwards. No flag was shown, so only a removal speaks: this
                # is how the safety of not waiting is read.
                overall["late_contradicted"] += 1
                if outcome == "removed":
                    overall["late_contradicted_removed"] += 1
            elif path == "both":
                overall["flagged"] += 1
                if outcome == "confirmed":
                    overall["flagged_confirmed"] += 1
                    shown["disputes_won"] += 1
                    if other is not None:
                        other["disputes_lost"] += 1
                elif outcome == "removed":
                    overall["flagged_removed"] += 1
                    shown["disputes_lost"] += 1

        # A provider that failed: before the reply (fallback_from, "openai:timeout")
        # or, on the fast path, after it (second_opinion "other_failed:timeout").
        for item in (row.get("fallback_from") or "").split(","):
            provider, _, label = item.strip().partition(":")
            failed(provider, label)
        if opinion.startswith("other_failed"):
            failed(OTHER_PROVIDER.get(row.get("provider") or ""), opinion.partition(":")[2])

    overall["wait_median_ms"] = _median(waits)
    overall["wait_p90_ms"] = _p90(waits)
    settled = overall["flagged_confirmed"] + overall["flagged_removed"]
    return {
        "days": days,
        "overall": overall,
        "models": models.rows(),
        "failures": failures,
        "settled": settled,
        "thin": settled < SETTLED_ENOUGH,
    }
