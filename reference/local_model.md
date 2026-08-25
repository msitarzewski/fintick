# Reference — the local model (Qwen 3.8 27B on Ollama)

This is FinTick's optional local inference route. It is loopback-only and requires no provider key.
Production aggregation defaults to Luna through external Hermes-managed OAuth; FinTick itself never
reads or stores that authentication state.

- Endpoint host: `http://localhost:11434`
- Model: **`qwen3.8:27b`**
- Two APIs available:
  - **Native, forced-JSON (recommended for extraction):** `POST /api/chat`
    ```json
    {
      "model": "qwen3.8:27b",
      "stream": false,
      "format": "json",                 // or a full JSON schema object to constrain fields
      "options": { "temperature": 0.1, "num_ctx": 4096 },
      "messages": [
        {"role":"system","content":"<your extraction instructions>"},
        {"role":"user","content":"<the headline>"}
      ]
    }
    ```
    Response: `resp["message"]["content"]` is a JSON string (parse it).
  - **OpenAI-compatible:** `POST /v1/chat/completions` (same message shape;
    reply at `choices[0].message.content`). Use whichever you prefer.

## ⚠️ It is a thinking model — two consequences

1. **It emits `<think>…</think>` before the answer.** With `/v1` you may see the reasoning in the
   content (or an empty answer if you capped tokens too low — see #2). Always **strip
   `<think>…</think>`** before parsing, and if you're not using `format:"json"`, extract the last
   `{...}` block from the content.
2. **Give it room.** A too-small `max_tokens`/`num_predict` gets spent entirely on hidden thinking
   and returns empty content. Use a generous limit (e.g. 512–1024 for a single-headline extraction).
   Using `/api/chat` with `"format":"json"` largely sidesteps the leakage because output is
   constrained to JSON.

## Practical guidance

- **Batch by item, not in bulk.** One headline per call keeps prompts tiny, JSON reliable, and a
  single bad response isolated. Wrap every call in try/except; on failure, record the error on that
  item and continue — never crash the pipeline.
- **Keep prompts tight and example-driven.** Tell it the exact JSON keys you want and that it must
  not invent tickers it isn't sure of (omit instead). Low temperature (0.1).
- **Throughput:** 27B dense on a 16 GB GPU is not instant, and thinking adds overhead. That's fine —
  the enricher runs continuously in the background and the tape shows raw headlines immediately,
  filling in enrichment as it completes. Don't block ingest on enrichment.
- This remains the optional local-provider path. Production aggregation defaults to Luna through
  Hermes-managed OAuth; benchmark both routes through `python3 -m fintick.benchmark`.

## Sanity check you can run

```bash
curl -s http://localhost:11434/api/chat -d '{
  "model":"qwen3.8:27b","stream":false,"format":"json",
  "options":{"temperature":0.1,"num_ctx":2048},
  "messages":[
    {"role":"system","content":"Extract JSON {\"symbol\":..,\"direction\":..} from a market headline."},
    {"role":"user","content":"NYMEX WTI crude October futures settle at $85.01 a barrel, down $2.05, 2.35%"}
  ]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["message"]["content"])'
```
Expect something like `{"symbol":"CL","direction":"down"}`.
