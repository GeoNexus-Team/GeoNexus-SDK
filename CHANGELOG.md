# Changelog

All notable changes to GeoNexus (the reference stack) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/) and the
project uses Semantic Versioning.

## [Unreleased]

### Added

- **GeoCard 1.1 sections: `composition` / `params` / `recipe_ref`** — a card can
  now describe a *whole solution* without containing one.
  - `composition.components[]` (`{id, role, step?, required?, alias?}`) lists the
    assets a composite case is made of, as **references only**: reusing a case
    moves no data, and each component keeps its own identity, access and licence.
    Duplicate `(id, role)` pairs are rejected — a duplicate reference would run a
    step twice.
  - `params.params[]` is the **parameter contract** (`ParamSpec`: name, type,
    required, default, enum, minimum/maximum, scope, target). Without it a forked
    case is inert: nobody knows which fields are safe to vary. `scope: fixed`
    publishes a *result* rather than a *method*.
  - `recipe_ref` points at the executable recipe (`recipe://ns/name@semver`),
    carrying the `data_snapshot` the solution was authored against.
  - Additive within 1.x: cards without the new sections stay valid, and the 0.1
    schema is still accepted. Conformance vectors grow with the sections
    (`tests/conformance/geocard_vectors.py`), and cross-field rules that JSON
    Schema cannot express (duplicate params, `minimum > maximum`, blank enum
    entries) are covered in `tests/test_geocard.py`.
  - `docs/GEOCARD.md` documents all three sections.

- **`geonexus.recipe` (v1.1)** — the executable half of a knowledge product:
  a **Recipe** says *how to run*, a GeoCard says *what it is*, and a `TaskGraph`
  is *one run*. No layer is a representation of another.
  - `Recipe` / `RecipeStep` / `RecipeOutput` with an address that carries
    identity (`recipe://<ns>/<name>@<semver>`), full DAG validation (unique ids,
    known dependencies, acyclicity, entrypoint, deliverables that point at real
    outputs, `step://` inputs that resolve).
  - `materialize(recipe, params, run_id=...)` → `RunPlan`: parameter contract
    first (`field`/`code` on every failure), then `${params.x}` substitution, then
    `task_id = "<run_id>:<step_id>"` so a replay is idempotent and two runs never
    collide. `provenance` on every task records recipe version, run id, resolved
    params and data snapshot.
  - `deliverables(plan)` returns the run's conclusions with roles — deliberately
    **no URLs**: where an artefact lives is the run environment's decision.
  - `RecipeRegistry`: file-backed catalogue reusing the card registry's review
    vocabulary, with `resolve()` picking the newest **approved** version
    (`version_key` avoids the `1.10.0 < 1.9.0` trap) and `fork()` as the
    executable definition of reuse — new address, your parameters, source
    untouched, fork returns as `pending`.
  - `geo.plan` (GeoMCP, **additive**, **read-only**): materialises a recipe over
    the wire without executing it. `geo.capabilities.recipe_planning` declares
    availability; `geo.describe` lists recipe summaries; errors `2005
    RECIPE_NOT_FOUND` / `2006 RECIPE_PARAM_INVALID` (with machine-readable
    `field`/`code`). Also exported through the MCP bridge as `geo_plan`.
  - CLI: `recipe validate|inspect|plan|register|search|fork` (`plan` exits `2` on
    a parameter-contract violation). `docs/RECIPE.md`,
    `examples/sdg_15_3_1/recipe.yaml`, `tests/test_recipe.py`,
    `tests/test_geomcp_plan.py`.

- **`geonexus.model` (v1.2, Phase 3)** — model invocation contract: a
  `type: model` GeoCard finally has an execution path.
  - `invoke_model(card, inputs, ...)` runs **compute gate → provider →
    I/O contract check**; `match_resource` is evaluated *before* the call, so
    an incompatible model is refused **with reasons** and nothing is sent.
  - `InferenceProvider` (synchronous HTTP) and `BatchProvider`
    (submit → poll → collect, with `submit`/`poll`/`collect` usable
    separately for long jobs).
  - Providers are selected from `card.access.protocol`; an unknown protocol
    (e.g. `grpc`) is an explicit error rather than a silent fallback.
  - `ModelOutcome` carries `error_code` / `retryable` / `reasons` /
    `compute_node`; failures are returned, not raised.
  - `ModelBackend` exposes model invocation as a `geotask`
    `ExecutionBackend`, so model tasks share the state machine, leases,
    retries and observability of every other task.
  - GeoMCP gains one **additive** method, `geo.invoke`; it is advertised in
    `geo.capabilities.model_invocation` and is a write method for
    `X-API-Key` purposes. Nodes that do not implement it answer with the
    standard `METHOD_NOT_FOUND` (-32601).
  - `docs/MODEL.md`, `tests/test_model.py` (52 tests).

- **`geonexus.geotask.scheduler` (v1.2, Phase 4)** — capacity accounting and
  admission control for task execution.
  - `Scheduler` / `Reservation` / `Occupancy` / `AdmissionResult`: admission
    by `cpu_cores` / `memory_mb` / `gpu_count` (+`gpu_kind`) reservations.
  - Three deliberately separate verdicts: **does not fit at all** →
    `no_compatible_compute` rejection with reasons; **no capacity right now**
    → `blocked` (re-enters the ready set once capacity is released);
    **fits and free** → reserve and run. Treating "does not fit" as queueing
    would queue forever, disguising a deployment error as resource pressure.
  - Priority ordering plus **per-owner rotation** so one submitter cannot
    starve another; backfill so a small task can use a large task's leftover
    capacity.
  - Retry backoff (`RetryPolicy.backoff_for`) defers re-admission and yields
    capacity to other tasks meanwhile; failure isolation at three boundaries
    (`submit`, `poll`, handler) so one bad task cannot take down a run.
  - `TaskGraph` learns the `blocked → queued` return path (the state machine
    edge always existed); `mark_blocked()` records the reason in the
    `state_changed` event and `task.message`.
  - Observability: `Scheduler.stats()`, `TaskRunner.scheduling()`,
    `RunReport.blocked` / `rejected` / `occupancy`.
  - `tests/test_geotask_scheduler.py` (49 tests), including an independent
    probe that measures real concurrency inside the handler and asserts the
    scheduler's accounting matches it exactly.

- **Docs consistency gate (v1.2, Phase 5)** — `scripts/check_docs.py` (run in
  CI) verifies that every public name listed in `docs/API_STABILITY.md` is
  really exported, that every `geonexus.*` dotted name in the docs resolves,
  that every `geo.<method>` is registered in `METHODS`, and that every doc
  link and in-repo path reference exists. `tests/test_docs_consistency.py`
  proves the checker can fail (one fake of each kind).
  New: `docs/MIGRATION.md` (old `TaskManager` / `federation` users).

### Changed

- `TaskGraph.ready()` now also returns `blocked` nodes whose dependencies are
  satisfied, so a task blocked on resources is retried once capacity frees.
  `mark_blocked()` is idempotent and carries a human-readable reason.
- `RunReport` gained `blocked`, `rejected` and `occupancy`; `TaskRunner`
  accepts an optional `scheduler=`. **Without a scheduler the previous
  behaviour is unchanged** (unlimited capacity).
- A `poll()` exception from an `ExecutionBackend` now fails only that task
  (`executor_lost`, retryable) instead of propagating out of `run()`.
- `API_STABILITY.md` documents `geonexus.geotask`, `geonexus.model` and the
  additive nature of `geo.invoke`.


- **`geonexus.web` (v1.1)** — Web backend-for-frontend layer:
  - JWT auth (`JWTConfig` HS256/RS256, stateless, distributed-friendly;
    `BearerAuth` / `APIKeyAuth` FastAPI dependencies).
  - Async `TaskManager` (background thread pool, `queued → running →
    done | failed | cancelled`, cooperative cancel, progress updates,
    pluggable persistence callback).
  - REST router (`create_web_router` / `create_web_app`): `/api/auth/login`,
    `/api/health`, `/api/cards`, `/api/skills`, `/api/nodes`,
    `/api/execute` (202 async), `/api/goals` (202 async), `/api/tasks`,
    `/api/tasks/{id}`, `/api/tasks/{id}/stream` (SSE),
    `/api/tasks/{id}/cancel`.
  - Node-level auth: `GeoMCPServer(api_keys=...)` gates `geo.execute` with
    `X-API-Key` (read methods stay open); `GeoMCPClient(api_key=...)` and
    `FederatedGeoMCPClient(api_key=...)` forward the header;
    `GeoMCPServer(forward_api_key=...)` for node-to-node delegation
    credentials (per-node key model).
  - CLI `geonexus web start` (`--registry`, `--node`, `--node-api-key`,
    `--cors-origin`, `--jwt-secret`).
  - `docs/WEB.md` (auth model, endpoints, deployment topologies, security).
  - Dependencies: `PyJWT>=2.8`, `cryptography>=42.0`.

- **Reflective GeoAgent (v1.1)** — LLM-assisted repair on the deterministic
  executor:
  - `plan_from_text_with_registry()` — registry-grounded goal translation
    (LLM can only pick skills that actually exist at the registry).
  - `PlanReflector` — LLM diagnosis of a failed plan step (multi-turn
    context: goal + error + completed-step summaries) producing a repair
    action: `retry` / `replace` / `skip` / `abort`.
  - `ReflectiveExecutor` — runs plans like `PlanExecutor`, reflects on
    failures and retries per advice, bounded by `max_reflections`; each
    step records its `reflections` history.
  - `evaluate_plan()` — LLM self-assessment of a finished plan
    (`satisfied` / `score` / `notes`).
  - `PlanStep.reflections` field + `skipped` status; `docs/AGENT.md`
    updated with the reflective-execution guide.

- **Web × Agent confluence (v1.1)** — `POST /api/goals` upgraded to the full
  reflective stack: registry-grounded planning → `ReflectiveExecutor`
  (LLM repairs failed steps) → `evaluate_plan` self-assessment. The task
  result carries `reflective` and `evaluation` fields; `"reflective": false`
  opts back into plain deterministic execution. `docs/WEB.md` documents the
  flow.

- **MCP client bridge (v1.1)** — `geonexus.mcp_client` imports tools from
  external MCP servers (stdio or Streamable HTTP) as GeoSkills:
  `MCPToolClient.stdio(...)` / `.http(...)` / `.list_tools()` /
  `.to_skills()` / `.register_into()`; `MCPTool.to_skill()` wraps a tool
  with a forwarding handler; `Skill.mcp_source` origin metadata;
  `GeoNode.register_skill_objects()` batch registration; CLI
  `geonexus mcp import`. Together with the server-side adapter this makes
  GeoNexus both an MCP host and client (verified end-to-end against our own
  `mcp run` server). `docs/MCP.md` client-side guide.

- **OGC coverage expansion (v1.1)** — three more OGC families as adapters:
  - **OGC API - Records** (`ogc_records.py`): catalogue discovery —
    `list_ogc_records` / `fetch_ogc_record` / `ogc_record_to_geocard`
    (themes → capabilities, keywords → tags, data links → access).
  - **OGC API - Tiles / Maps / Styles** (`ogc_tiles.py`): visualization
    plane — `list_ogc_tilesets` / `ogc_tileset_to_geocard` (`tiles`
    capability, `{z}/{y}/{x}` endpoint), `list_ogc_styles` /
    `ogc_style_to_geocard` (`styling` capability),
    `ogc_visualization_to_geocards`.
  - **WMS / WMTS** (`ogc_wms.py`): legacy services — GetCapabilities
    parsing (xmltodict or dependency-free light parser),
    `list_wms_layers` / `wms_layer_to_geocard` (GetMap),
    `list_wmts_layers` / `wmts_layer_to_geocard` (GetTile).
  - `docs/OGC.md` V1.1 section; 18 new tests (records 11 + tiles/styles/WMS
    +7).
  - CLI: `geonexus card import-ogc-records [--record --collection --bbox
    --query --limit]`, `import-ogc-tiles [--tileset --style]`,
    `import-ogc-legacy [--service wms|wmts]`; 3 CLI tests.

- **Resource matching (v1.1)** — `geonexus.resource` coordinates compute
  resources: `ComputeCapability` (what a `type: compute` node provides),
  `match_resource(model_card, capabilities)` (checks `runtime.cpu` /
  `runtime.memory` / `runtime.gpu` requirements against nodes, with
  per-requirement reasons), `resolve_compute_capabilities(registry_url)`
  (discovers `type: compute` cards), `parse_memory` (`"4Gi"` → GiB).
  Enables routing a model to a node that satisfies its runtime — the
  "coordinate data, model and compute" step of the GeoCard workflow.
  `docs/RESOURCE.md` guide; 16 tests.

- **Data registration & review workflow (v1.1)** — upload a file → generate
  a GeoCard → submit for review → approve → discoverable:
  - `geonexus.metadata`: `inspect_raster` (CRS/bounds/resolution/bands from
    any rasterio-readable raster), `raster_stats` (nodata-aware band stats),
    `raster_to_geocard` (auto-builds a GeoCard with spatial/bands/access
    pointing at the file). 8 tests.
  - Registry review state machine: `RegistryEntry.status ∈ {pending,
    approved, rejected}` (default `approved` — existing registrations stay
    compatible); store `set_status/approve/reject` + `list_entries(status)`;
    `search`/`list` return only approved by default (`status="all"` or an
    explicit state for admin/review views); server
    `GET /cards?status=`, `POST /cards/{id}/approve|reject`;
    client `register(status=)` / `approve` / `reject` / `list_cards(status=)`.
    9 tests.

## [1.0.0] - 2026-08-21

### Added

- **GeoCard 1.0** — schema `geocard_version` now accepts both `0.1` and
  `1.0` (legacy cards stay valid); the SDK emits `1.0` by default.
- **GeoMCP 1.0** — protocol version advertised as `1.0.0`; method set and
  error code registry frozen for the 1.x line.
- **Conformance suites** (`tests/conformance/`): GeoCard validation vectors
  (6 valid / 14 invalid cases) and GeoMCP JSON-RPC vectors (11 request
  cases + 4 invalid envelopes), run independently of the SDK.
- **STAC write side** — `geocard_to_stac_item` / `geocard_to_stac_catalog` /
  `save_stac_item`, CLI `geonexus card export-stac`, `geonexus:…` extension
  fields, `demo stac-write` (`docs/STAC.md`).
- **GGIHS dashboard** — `GET /` single-file HTML dashboard on the GGIHS
  service (`/summary`, `/nodes`, `/catalog` aggregation).
- **Registry federation** — pull-based peer sync (`RegistryFederator`,
  `registry start --peer`, `registry sync`, `POST /sync`).
- **GeoNode-to-GeoNode federation** — node-level delegation with
  `__geonode_delegate` loop guard, health-aware routing (`GET /nodes`,
  `--health-probe`), `demo federation-deep`.
- **Governance docs** — `docs/API_STABILITY.md` (frozen public API +
  deprecation policy), `docs/RELEASE.md` (release process), this changelog.

### Changed

- Package, GeoCard schema and GeoMCP protocol versioned to 1.0.0.
- `docs/GEOCARD.md` and `docs/GEOMCP.md` promoted to formal specifications
  (versioning, extension policy, per-method schemas, error code registry,
  transport bindings, conformance statements).

### Fixed

- IOField `required` no longer emitted when `False` (schema-valid output for
  `outputs` sections).
- Registry `extent.spatial.crs` handling for string-vs-list CRS (pygeoapi
  interop); job results discovery via qualified OGC `rel` URIs.

### Released

- **v1.0.0 published to PyPI as `geonexus-sdk`** (sdist + wheel; the bare
  `geonexus` name is occupied by an unrelated package, the import package
  stays `geonexus`). Rehearsed on Test PyPI (`geonexus==1.0.0`) and verified
  from a clean venv: `pip install geonexus-sdk` → `import geonexus` →
  `geonexus version` reports 1.0.0. See `docs/RELEASE.md`.

## [0.1.0] - 2026-08-19

### Added

- GeoCard schema 0.1 + Python SDK (model/builder/loader/validator,
  ContractValidator).
- GeoMCP 0.1.0 — JSON-RPC 2.0 protocol, FastAPI server, httpx client.
- Local GeoNode (GeoCard registry, Skill registry, local runtime),
  GeoSkill abstraction, `geonexus` CLI.
- Amazon NDVI demo (synthetic data), real Sentinel-2 STAC demo,
  federated/pipeline/agent/OGC demos.
- Shared GeoCard Registry with persistence + API-key auth; GeoAgent planner
  (DAG pipelines, LLM goal translation); official MCP SDK bridge
  (stdio + Streamable HTTP); STAC/OGC API adapters (read).
- CI (pytest + ruff + mypy + build on 3.11/3.12), wheel packaging with the
  GeoCard schema shipped as package data.
