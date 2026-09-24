# Recipe specification (v1.0)

A **Recipe** is the executable half of a knowledge product: it says *how to run*.
The **GeoCard** it is attached to says *what the thing is*. Neither is a
representation of the other, and a run is neither — it is an instance.

```
GeoCard (facade)          Recipe (method)              TaskGraph (one run)
─────────────────         ───────────────────          ────────────────────
composition: refs   ───▶  steps + params   ───▶  Recipe + params + run_id
params: contract    ◀───  params: contract                 │
recipe_ref ─────────────▶ id                                          ▼
                                                       outputs (deliverables)
```

| Question | Answered by | Not answered by |
|---|---|---|
| What is it, who owns it, what may I do with it? | GeoCard | Recipe |
| Which assets does the solution consist of? | GeoCard `composition` | Recipe |
| Which fields may I change, within what bounds? | both — **one** definition (`ParamSpec`) | anything else |
| What are the steps and their order? | Recipe `steps` | GeoCard (a card holds no DAG) |
| What did *this* run produce? | TaskGraph + run outputs | GeoCard, Recipe |

## Address

```
recipe://<namespace>/<name>[@<semver>]
```

The version is part of the identity, not a field beside it: `@1.0.0` and `@1.1.0`
are two independently approvable records, which is what makes it safe to publish
a new version while someone is still running the old one. When a caller omits
the version, resolution picks the **newest approved** version — deterministically,
using semantic-version ordering (`version_key`), so `1.10.0 > 1.9.0`.

## Document

```yaml
recipe_version: "1.0"
id: recipe://geonexus/sdg-15-3-1@1.0.0
name: SDG 15.3.1 土地退化评估
description: 2 个数据 + 1 个算子 + 1 份报告。
license: CC-BY-4.0
tags: [sdg, land-degradation]

params:                    # the parameter contract (same ParamSpec as GeoCard)
  - name: year
    type: integer
    required: true
    minimum: 2015
    maximum: 2024
    target: s2.params.year
  - name: region
    type: enum
    enum: [蒙古, 墨西哥]
    default: 蒙古
  - name: baseline
    type: integer
    scope: fixed           # pinned by the author: reproducibility, not a knob
    default: 2015

requires:                  # cards this recipe expects to resolve (pre-flight)
  - geocard.packed.menggu

steps:
  - id: s1
    kind: skill            # skill | model | batch | composite
    uses: geocard.packed.menggu
    params: {region: "${params.region}"}
    outputs: {aoi: 矢量边界, mask: 掩膜}

  - id: s2
    kind: skill
    uses: 2026.9.16 SDG15.3.1
    depends_on: [s1]
    params: {year: "${params.year}", baseline: "${params.baseline}"}
    inputs: {mask: "step://s1/mask"}
    outputs: {degradation: 退化栅格, transition: 转移矩阵}
    resources: {cpu: 4, memory_mb: 8192}
    timeout_seconds: 1800
    max_attempts: 1        # EXTRA attempts; becomes RetryPolicy.max_attempts = 2

  - id: s3
    kind: skill
    uses: report.sdg1531.mng.2019
    depends_on: [s2]
    inputs: {degradation: "step://s2/degradation"}
    outputs: {report: 报告文件}

entrypoint: s1
outputs:                   # what this recipe DELIVERS
  - name: degradation_map
    from: s2.degradation
    role: data
  - name: report
    from: s3.report
    role: knowledge
    media_type: text/html

data_snapshot: {geokg: "2026.09.1", lc08: "2024.12"}
```

### `steps`

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique, non-empty. Enters `task_id` as `<run_id>:<step_id>`. |
| `uses` | string | A GeoCard id, a registered skill name, or a nested `recipe://` address. |
| `kind` | string | `skill` \| `model` \| `batch` \| `composite`. Must be `composite` exactly when `uses` is a recipe. |
| `depends_on` | string[] | Step ids. Unknown, self, or cyclic references are rejected. |
| `params` | object | Literals or `${params.<name>}` templates. |
| `inputs` | object | Asset ref, `${params.<name>}`, or `step://<step>/<output>` to consume an upstream result. |
| `outputs` | object | `name → description`. A ref is published as `step://<step_id>/<name>`. |
| `resources` | object | `cpu`, `memory_mb`, `gpu` — a **reservation**, accounting unit MB. |
| `timeout_seconds` | number | Per-step timeout; must be positive. |
| `max_attempts` | integer | **Extra** attempts after a retryable failure (`0` = try once). |

### `outputs` (deliverables)

A deliverable is not a side effect of a step, it is the **conclusion** of the
solution. `from` must be `<step>.<output>` and the step must actually declare
that output — otherwise "delivery" is just a wish. `role` uses the GeoCard role
vocabulary so the platform does not need a second taxonomy; a report is
`role: knowledge`, not a new asset type.

## Parameter contract

Parameters are declared **once**, using the same `ParamSpec` as GeoCard `params`:
the card can then reference the contract instead of restating it, and there is no
way for "the card says reusable" and "the recipe says locked" to both be true.

Resolution order for each parameter:

1. `scope: fixed` → the author's `default`, always. Supplying the *same* value
   again is allowed (a form that round-trips the field would otherwise be
   unusable); supplying a *different* value raises `param_fixed`.
2. Supplied by the caller → coerced, then checked against `enum` / `minimum` /
   `maximum`.
3. Otherwise the `default`.
4. Otherwise `null` — unless `required`, which raises `param_missing`.

Failures raise `RecipeParamError` carrying a machine-readable `field` and `code`,
which is what the platform turns into a 400 and a highlighted form input:

| `code` | Meaning |
|---|---|
| `param_missing` | A required parameter was not supplied. |
| `param_unknown` | The recipe has no such parameter. |
| `param_invalid_type` | Not convertible to the declared type (unambiguously). |
| `param_invalid_enum` | Outside the declared `enum`. |
| `param_out_of_range` | Outside `[minimum, maximum]`. |
| `param_fixed` | Attempt to override a `scope: fixed` parameter. |

**Coercion is deliberately narrow.** `"2019"` → `2019` is accepted: JSON forms
and shells send numbers as strings. `"abc"` → `int` is rejected: that means the
caller misunderstood the parameter, and guessing would hide the mistake.

## Materialisation

`materialize(recipe, params, run_id=...)` produces a `RunPlan`:

* `task_id = f"{run_id}:{step_id}"` — a run's tasks are idempotent keys
  (`docs/GEOTASK.md` §5.4), so replays do not duplicate work and two runs never
  collide.
* `${params.x}` is substituted **here**, never in the stored document. A recipe on
  disk stays parameterised; that is what makes reuse real rather than "edit
  someone else's file". A whole-string placeholder keeps its type (an `integer`
  parameter yields an `int`, not `"2021"`); inside a larger string it is
  interpolated as text.
* `step://<step>/<output>` becomes `TaskIO(kind="task_output", from_task=...,
  field=...)`; anything else becomes `TaskIO(kind="asset")`.
* `provenance` on every task records the recipe id, version, `run_id`, the
  resolved parameter values and the `data_snapshot` — so a result can be told
  apart from "the data moved under it".
* `resolve` is an optional hook mapping `uses` → a concrete reference
  (`asset://`, `s3://`, a node endpoint). Without it the recipe stays decoupled
  from any deployment.

`deliverables(plan)` returns the delivery list. It deliberately produces no URLs:
**where an artefact lives is decided by the run environment**, the SDK only
guarantees which step's output is this run's conclusion.

## Catalogue, review and fork

`RecipeRegistry` is a thread-safe, optionally file-backed catalogue. It reuses
the card registry's review vocabulary (`pending` / `approved` / `rejected`) and
appends to a review history rather than overwriting it.

Approval **authority** is not here. `approve`/`reject` record state and history;
who is allowed to call them is the platform's decision (SDK must not invent an
identity system).

`fork` is the executable definition of "reuse": take an approved recipe, change
namespace/name/version, set defaults on reusable parameters, pin others as
`fixed`, and get a **new, independently approvable** recipe. The source is not
touched. Injected defaults are coerced through the parameter contract, so
`--set year=2021` from a shell stores `2021`, not `"2021"`.

```python
forks = RecipeRegistry("recipes.json")
forked = forks.fork(
    "recipe://geonexus/sdg-15-3-1",
    name="sdg-15-3-1-mexico",
    params={"region": "墨西哥"},
    fixed={"year": 2019},
)
plan = materialize(forks.require(forked.id, approved_only=True).recipe, {"year": 2021}, run_id="r2")
plan.output_refs()   # {'degradation_map': 'step://s2/degradation', 'report': 'step://s3/report'}
```

## GeoMCP surface

`geo.plan` (V1.1, **additive**) materialises `recipe + params` into a run without
executing anything. It is read-only, so it needs no API key even on a node that
requires one for `geo.execute`.

* `geo.capabilities` advertises it under `methods` and describes availability in
  `recipe_planning` (`{enabled, method, read_only, recipes}`) — the same
  "declare before you call" pattern as `geo.invoke`.
* `geo.describe` accepts `recipes: [...]` and returns **summaries** (id, name,
  version, params, deliverables), not whole recipes; `geo.plan` returns the full
  recipe alongside the task graph.
* Error codes: `2005 RECIPE_NOT_FOUND` (no such recipe, or not approved),
  `2006 RECIPE_PARAM_INVALID` (parameter contract violated; `data` carries
  `field` / `code` / `message`).
* A node without a `recipe_registry` answers `2005` with
  `{"reason": "recipe_registry_not_configured"}` rather than returning an empty
  plan.

A conforming client that does not know `geo.plan` sees it in `methods` and
ignores it; an implementation that does not provide it returns the JSON-RPC
`METHOD_NOT_FOUND` (-32601). No existing method changed semantics.

## CLI

```bash
geonexus recipe validate examples/sdg_15_3_1/recipe.yaml
geonexus recipe inspect  examples/sdg_15_3_1/recipe.yaml
geonexus recipe plan     examples/sdg_15_3_1/recipe.yaml --param year=2021 --run-id demo-1
geonexus recipe register examples/sdg_15_3_1/recipe.yaml --catalogue recipes.json
geonexus recipe search   --catalogue recipes.json --param year --approved-only
geonexus recipe fork     recipe://geonexus/sdg-15-3-1 --catalogue recipes.json \
                         --name sdg-15-3-1-mexico --set region=墨西哥 --fix year=2019
```

`recipe plan` exits `2` on a parameter-contract violation and prints
`field` / `code`, so a script can react to the specific problem.

## Validation rules (all enforced, all tested)

* `id` matches `recipe://<ns>/<name>[@<version>]`; `recipe_version` major must be
  supported.
* At least one step; step ids unique and non-empty; `kind` from the vocabulary.
* Every `depends_on` target exists, is not self, and the graph is acyclic.
* `entrypoint`, when set, names a step.
* Deliverable names are unique; each `from` names an existing step and a declared
  output of it.
* Every `step://<step>/<output>` input names an existing step and (when the
  producer declares outputs) a declared output.
* Every `${params.x}` refers to a declared parameter.
* A composite step nests a recipe and only a composite step nests a recipe.
* `timeout_seconds > 0`; `max_attempts >= 0`.

There is **no separate JSON Schema** for recipes, on purpose: unlike GeoCard
(whose schema is the cross-language executable spec), recipes are consumed
through the SDK — `geo.plan`, the CLI and the Platform all call the same
`Recipe` model over GeoMCP. A second copy of these rules would be a second
answer to the same question.
