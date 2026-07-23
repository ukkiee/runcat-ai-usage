# runcat-ai-usage

Tracks how much of each AI coding provider's rate limit has been consumed, and surfaces it in RunCat Neo. The language below is what we use when talking about limits, readings, and what the user ends up looking at.

## Language

### Providers and their limits

**Provider**:
A service whose rate-limit usage this tool tracks. Currently Claude Code and Codex.
_Avoid_: vendor, backend, service, source

**Plan**:
The subscription tier that determines a Provider's limits — e.g. "Max 20x", "Pro 5x".
_Avoid_: tier, subscription, subscriptionType

**Window**:
A rate-limit period, carrying how much of it has been used and the moment it resets. A Provider exposes several at once.
_Avoid_: limit, quota, bucket, period

**Session Window**:
The short rolling Window, five hours wide.
_Avoid_: 5h, hourly, current

**Weekly Window**:
The account-wide Window, seven days wide.
_Avoid_: 7d, long-term

**Model-Scoped Window**:
A seven-day Window that applies to one model only — e.g. Fable. A Provider may expose several, or none.
_Avoid_: per-model limit, scoped quota

**Reset**:
The moment a Window's used share returns to zero. Distinct from refreshing a credential.
_Avoid_: refresh, rollover, expiry

### What we record and show

**Usage Reading**:
A provider-neutral record of every Window for one Provider at one moment. The thing we persist; everything the user sees is derived from it.
_Avoid_: snapshot, usage data, metrics, state

**Stale Reading**:
A Usage Reading we could not replace because the Provider was unreachable or its credential had expired. Windows whose Reset has since passed count as zero.
_Avoid_: fallback, cached usage, last-good

**Card**:
What RunCat Neo shows for one Provider — the rendered presentation of a Usage Reading, plus the short text in the menu bar.
_Avoid_: snapshot, widget, tile, dashboard entry
