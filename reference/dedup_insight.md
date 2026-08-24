# Reference — the duplication problem (why dedup is mandatory)

`fintwitter` frequently posts the **same headline multiple times within seconds**, differing only
by capitalization/punctuation. Real example from the live feed, three posts in ~28 seconds:

```
18:51:54  BRENT CRUDE FUTURES SETTLE AT $92.17/BBL, DOWN $2.22, OR 2.35%
18:51:52  BRENT CRUDE FUTURES SETTLE AT $92.17/BBL, DOWN $2.22, OR 2.35%
18:51:26  Brent crude futures settle at $92.17/bbl, down $2.22, 2.35%.
```

All three are the *same event*. A tape that shows all three is broken. Even in a **quiet** 60-post
sample, 5 posts were exact-normalized repeats of another; during news bursts the duplicate rate is
much higher.

## Approach that works

1. **Normalize** each post's text: lowercase → collapse all whitespace to single spaces → strip
   trailing punctuation (`. , - – — : ; ! ?`). (Consider also stripping a leading source tag if
   one appears, but start simple.)
2. **Hash** the normalized string (e.g. sha1).
3. On insert, look for an **earlier** post with the same hash within a **time window**
   (a rolling window of ~30–60 min is plenty — duplicates cluster within seconds, but a generous
   window is safe). If found, mark the new row as a **duplicate** pointing at the first
   ("canonical") post's `uri`; otherwise it's canonical.
4. Keep duplicate rows in the DB (auditing / dup counts) but **only canonical rows feed the tape
   and the enricher** — never enrich the same headline twice.

## Watch out for

- The three variants above normalize to two distinct strings (the third drops "OR" and adds a
  period): `"...down $2.22, or 2.35%"` vs `"...down $2.22, 2.35%"`. Exact-normalized hashing
  catches the identical pair; the near-variant is a *different* string. That's acceptable for v1
  — do **exact normalized-hash** dedup first and ship it. A fuzzy/semantic near-dup pass
  (e.g. same numbers + same head noun) is a nice **stretch**, not required for acceptance.
- Don't dedup across genuinely different events that happen to share boilerplate — key on the full
  normalized text, not a prefix.
