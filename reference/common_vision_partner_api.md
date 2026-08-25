# common.vision Partner API contract used by FinTick

Credential-free implementation reference derived from the supplied **common.vision Partner API**
OpenAPI 3.0.3 document (`info.version: 1.0.0`). The live API remains the authority if this note
becomes stale.

## Connection

- Production base: `https://common.vision/api/v1`
- Authentication: HTTP Bearer token (Sanctum API token)
- FinTick endpoint: `GET /articles`
- Runtime credential: `FINTICK_COMMON_VISION_TOKEN` supplied outside the repository

FinTick sends:

- `search`: compact claim anchors, maximum API length 255
- `from` / `to`: ISO calendar dates bounding the search
- `per_page`: 1–100; FinTick requests only the bounded candidate count it needs

The token must never be written to source, logs, documentation, process arguments, or tests.

## Article-list response

`GET /articles` returns a paginated object:

- `data[]`: Article records
- `meta`: `total`, `per_page`, `current_page`, `last_page`, `from`, `to`
- `links`: `first`, `last`, nullable `prev`, nullable `next`

FinTick uses these Article fields:

- `title`
- `url` — canonical external candidate URL
- `published_at` — date-time used for lead-time calculation after validation
- `metadata.source` — actual publisher identity
- `feed.name` — ingestion feed name
- `feed.url` — ingestion feed URL
- `feed.feed_type` — `rss`, `atom`, or `json`

Publisher and feed are deliberately separate. A publisher article discovered through a Google News
feed is one external candidate, not independent evidence from both the publisher and Google News.
The `feed_*` fields are provenance, not additional corroborating sources.

All API content is untrusted candidate evidence. It must still pass FinTick's URL, social-host,
timestamp, claim-specificity, and stance checks before it can affect event status.

## Error and rate-limit behavior

Documented response classes include:

- `401` unauthorized
- `403` inactive/forbidden partner access
- `404` missing resource
- `422` validation error
- `429` rate limit exceeded
- `500` server error

The rate-limit body is:

- `message`: string
- `error`: `rate_limit_exceeded`
- `retry_after`: integer seconds

FinTick performs one bounded retry for `429`, preferring the HTTP `Retry-After` header when present
and otherwise using JSON `retry_after`. The delay is clamped to 1–60 seconds. Other HTTP failures,
malformed JSON, and responses without a `data` array fail closed; they never promote an event.

## Other documented resources

The contract also exposes article detail/related lookup, feeds, trending tags/entities/topics/
opportunities, and product mention/popularity endpoints. FinTick intentionally does not use those
surfaces in the validation pipeline.
