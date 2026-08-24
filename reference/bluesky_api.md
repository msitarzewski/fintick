# Reference — Bluesky public API (no auth needed)

The `fintwitter.bsky.social` feed is read through Bluesky's **public AppView**. No login, no key,
no rate-limit auth. Be polite (a few requests per poll; back off on HTTP 429).

- Base: `https://public.api.bsky.app/xrpc`
- Account handle: `fintwitter.bsky.social`
- Resolved DID: `did:plc:43fdk46qa5gsokzygzildsaq`
  (resolve yourself via `com.atproto.identity.resolveHandle?handle=<handle>` — see `did.json`)

## The endpoint you need

```
GET /app.bsky.feed.getAuthorFeed
      ?actor=fintwitter.bsky.social      # handle or DID both work
      &limit=100                          # max 100
      &filter=posts_no_replies            # original posts only (what the tape wants)
      &cursor=<opaque>                    # omit for newest page; pass to page older
```

## Response shape (trimmed to what matters)

```json
{
  "feed": [
    {
      "post": {
        "uri": "at://did:plc:.../app.bsky.feed.post/3l...",   // PRIMARY KEY (globally unique)
        "cid": "bafyrei...",
        "record": {
          "text": "BRENT CRUDE FUTURES SETTLE AT $92.17/BBL, DOWN $2.22, OR 2.35%",
          "createdAt": "2026-08-24T18:51:54.812Z",             // author clock
          "langs": ["en"]
        },
        "indexedAt": "2026-08-24T18:51:55.001Z",               // bsky clock
        "likeCount": 0, "repostCount": 0, "replyCount": 0,
        "embed": { "$type": "app.bsky.embed.images#view", ... } // optional; may be absent
      }
    }
  ],
  "cursor": "2026-08-24T18:19:56Z::bafy..."                    // absent on the last page
}
```

## Ingest algorithm (recommended)

1. Fetch newest page (no cursor).
2. Insert posts you haven't seen (key on `uri`). Re-inserting a known `uri` is a no-op → idempotent.
3. If the whole page was already known, you're caught up — stop.
4. Otherwise follow `cursor` to the next (older) page and repeat, up to a sane page cap
   (e.g. 8 pages = 800 posts) so a long outage doesn't page forever.
5. Persist the newest `cursor`/timestamp or the newest seen `uri` as your high-water mark.

## Notes

- The feed is **bursty and high-volume** during market hours; a 15-minute poll with
  pagination-to-last-seen catches everything comfortably.
- `reference/feed_sample.json` is a real 60-post capture (with a `cursor`) — build and test the
  whole pipeline against it **offline** before hitting the network.
- Some posts carry image embeds; the tape is text-first, but keep `embed.$type` if present.
