# Request budget

Onshape returns `429 Too Many Requests` with `Retry-After` when an endpoint's window is used up and reports
`X-Rate-Limit-Remaining` on every response. On 2026-10-25 a modelling session hit 429 on
`/partstudios/.../features` after roughly 200 Part Studio requests in 20 minutes; `Retry-After` was about
21,000 s, and `/partstudios/.../featurescript` was blocked until the same moment while `/featurestudios` and
`/documents` kept working. The bridge therefore budgets per endpoint family (first path segment after `/api/vN/`).

What the bridge does:

- **Ledger and guard** (`client.py`): every REST response updates the family's request count, last
  `X-Rate-Limit-Remaining` and any `Retry-After` deadline (persisted in `<runtime>/ratelimit.json`).
  While a family is blocked, requests fail locally with the reset time and are not sent.
  `bridge_status` returns the ledger as `budget`.
- **Immutable cache**: GET responses and read-only evaluates on `/m/{microversion}` or `/v/{version}`
  paths are stored under `<runtime>/artifacts/immutable/` and reused.
- **Write-derived snapshots**: `feature` folds each response (feature, state, microversion) into a new
  snapshot, so the next write or evaluate needs no `inspect_model`. After an edit, downstream feature
  states are marked `UNVERIFIED` until re-read.
- **Tab freshness**: `inspect_model(previous=...)` asks the open editor tab for its microversion (local,
  no REST). It trusts the tab only if it matches the previous snapshot and no newer revision was seen in
  this server's own write responses.
- **Fewer requests per tool**: inspect geometry is one evaluate (names included) instead of parts + evaluate;
  `sidebar_edit` feature writes reuse the element_tree feature list and verify from the write response plus
  the open tab instead of a full REST capture.
- **Local bodies**: `api_request`/`api_write` accept `body_file`, and `featurestudio_patch` accepts
  `contents_file`, from `<runtime>/artifacts/uploads`, folders listed in `<runtime>/body_roots.txt`, or
  `ONSHAPE_NATIVE_BODY_ROOTS`. `featurestudio_patch` also does exact find/replace edits.

## Round 2 (0.6.0)

What Onshape documents (it does not publish the numbers): limits are per endpoint and per fixed time window;
every response carries `X-Rate-Limit-Remaining`, the calls left for that endpoint in the current window;
a 429 carries `Retry-After`, the seconds until that counter resets (so a 429 does not extend the wait).
Browser-session calls do not count against the annual API allocation, but they are rate-limited.
Sources: onshape-public.github.io/docs/api-adv/errors and /docs/auth/limits.

- **Per-endpoint ledger**: `remaining` is tracked per endpoint key (`GET partstudios/features`) with the
  largest value seen (~ the window allowance) and the last 40 samples, so the refill behaviour can be learned.
  A rising counter is logged as a reset; a 429 sets the family's reset time.
- **Reserve**: when an endpoint has `reserve` (default 5) or fewer calls left in the current window, requests
  fail locally ("Nothing was sent"). `bridge_status(pacing={"reserve": 0})` lowers it until the MCP server
  restarts; defaults that survive restarts go in `<runtime>/pacing.json` (edit by hand). A reading counts only
  for its window: readings older than a passed Retry-After deadline or an observed reset are ignored, and
  readings with no known reset expire after 15 minutes.
- **Burst limit**: a token bucket per family (default 30, refill 30/min, waits up to 20 s) catches runaway
  loops. It does not replace the window budget.
- **Low-budget hints**: results of inspect/evaluate/feature/api tools include `budget_low` when an endpoint is
  at <= 20 calls or <= 15% of its largest seen value.
- **ETag revalidation**: mutable GETs remember the ETag and send `If-None-Match`; a 304 returns the stored body.
  Whether Onshape counts a 304 against the window is visible in `bridge_status` (`revalidated_304`).
- **Batched edits**: several parameter/suppression edits in one `feature` call are one
  `POST .../features/updates` (updateFeatures). HTTP 400 from that call (nothing applied) falls back to
  per-feature updates (then reported as `batch_fallback`: no longer all-or-nothing). Renames, adds and sketch
  edits stay sequential; sketches always send their full definition.
- **Report**: `evaluate(preset="report")` and `feature(..., report=true)` return feature errors/warnings,
  solids and published variables (`getVariable`, default `checksText`) from one pinned evaluate and update
  the snapshot, replacing a separate inspect. UNVERIFIED features become OK only when the script checked every
  feature; a failed report never hides a successful write (`report_error`). Still to confirm against Onshape:
  that `getFeatureError` reports regeneration errors inside an evaluate.
- **Feature-spec cache**: `feature_template` caches standard specs by FeatureScript `libraryVersion` and
  custom specs by namespace (`e<eid>::m<microversion>` is immutable); same-document custom specs come from
  the Feature Studio endpoint. Edits no longer invalidate it.
- **export**: STL from one pinned tessellation read with a watertight check; STEP polls its translation
  inside the tool with backoff (3, 3, 4, 5, 8, 10, 15 s).
- **Unknown outcomes**: when a write times out or the bridge drops, the workspace is marked UNKNOWN so the
  next `inspect_model` re-reads over REST instead of trusting the open tab.
- **Extension gate**: after a bridge timeout, reads wait up to 20 s for the earlier command; writes are
  still refused while its outcome is unknown.

Bulk geometry writes stay on the public REST API; the editor's internal connection is used only for
operations REST lacks (sidebar folders, display) and for local reads of the open tab.
