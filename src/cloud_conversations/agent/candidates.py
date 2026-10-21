"""Search results for free hardware, as prompt text."""

from __future__ import annotations

from cloud_conversations.schema.search import Candidates


def render(candidates: Candidates, limit: int) -> str:
    """The search results, most free first, at most `limit` rows."""
    hours = (candidates.end - candidates.start).total_seconds() / 3600
    when = (
        "as soon as possible"
        if candidates.query.start_at is None
        else candidates.start.isoformat()
    )
    lines = [f"Free for {hours:g}h from {when}:"]

    ranked = sorted(candidates.items, key=lambda c: (-c.free, c.site, c.compute))
    if not ranked:
        lines.append("  nothing matched those filters")
    lines += [f"  {c.site} {c.compute}: {c.free} free" for c in ranked[:limit]]
    if len(ranked) > limit:
        lines.append(f"  ... and {len(ranked) - limit} more, all with fewer free")
    return "\n".join(lines)
