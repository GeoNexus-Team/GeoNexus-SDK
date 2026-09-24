# GeoCard specification (v1.0)

> GeoCard is *"machine-readable identity, capability and contract description
> for a geospatial asset."*

**Normative status:** this document and
[`schemas/geocard.schema.json`](../schemas/geocard.schema.json) (JSON Schema
Draft 2020-12) are the GeoCard specification. A GeoCard **conforms** when it
validates against the schema. The conformance vector suite in
`tests/conformance/` is the executable form of this spec — a conforming
implementation must pass the same vectors.

## Versioning & extension policy

- `geocard_version` accepts **`0.1`** (legacy) and **`1.0`** (current); the
  SDK emits `1.0`. Cards written as `0.1` remain valid — a 1.0 implementation
  MUST accept them.
- The top level allows `additionalProperties` **for extension only**: an
  unknown top-level key is permitted and MUST be an object or array
  (namespace your extensions, e.g. `geonexus:*`). Known sections are
  validated strictly (`additionalProperties: false`).
- Spec changes: additive (new optional sections/fields) are allowed within
  1.x; removing or changing semantics requires a MAJOR version.
- Deprecation policy: see `docs/API_STABILITY.md`.

## Top-level (required)

| Field              | Type   | Description                                             |
|--------------------|--------|---------------------------------------------------------|
| `geocard_version`  | string | GeoCard spec version — `"0.1"` (legacy) or `"1.0"`.     |
| `id`               | string | Stable unique identifier (`^[A-Za-z0-9][A-Za-z0-9._-]*$`). |
| `type`             | string | One of `data`, `model`, `skill`, `agent`, `workflow`, `knowledge`, `compute`. |
| `name`             | string | Human-readable display name.                            |
| `description`      | string | Free-text description.                                  |
| `tags`             | [string] | Optional discovery tags.                               |

## Sections (all optional)

### `spatial`

| Field        | Type          | Description                                        |
|--------------|---------------|----------------------------------------------------|
| `bbox`       | [number] (4-6)| `[west, south, east, north]` (2D) or 3D variant.    |
| `crs`        | string        | e.g. `"EPSG:4326"`, `"EPSG:3857"`.                 |
| `resolution` | number (>=0)  | Best spatial resolution in metres.                  |
| `geometry`   | string        | Optional GeoJSON geometry footprint.                |

### `temporal`

| Field      | Type   | Description                              |
|------------|--------|------------------------------------------|
| `start`    | string | ISO 8601 start of coverage.              |
| `end`      | string | ISO 8601 end of coverage.                |
| `interval` | string | Sampling interval, e.g. `"P16D"`, `"P5D"`.|

### `bands`

Array of `{name, dtype?, units?, description?}` — spectral/thematic bands.

### `inputs` / `outputs`

Arrays of `{name, type, description?, required?}` — declared interface of a
skill, model, workflow or compute asset. `type` examples: `raster`,
`vector`, `number`, `string`, `array`, `object`.

### `capabilities`

Array of capability names (strings) or `{name, description?}` objects, e.g.
`["ndvi", "change-detection"]`.

### `access`

| Field      | Type   | Description                                      |
|------------|--------|--------------------------------------------------|
| `protocol` | string | e.g. `geomcp`, `https`, `s3`, `stac`, `ogcapi`.  |
| `endpoint` | string | URL or endpoint identifier.                      |
| `auth`     | string | e.g. `none`, `apikey`, `oauth2`.                 |
| `format`   | string | e.g. `GeoTIFF`, `COG`, `Zarr`, `GeoJSON`.        |

### `provenance`

`{provider?, source?, lineage?}` — where the asset came from.

### `license`

Either an SPDX string (`"CC-BY-4.0"`) or `{name?, url?}`.

### `compliance`

| Field          | Type     | Description                                            |
|----------------|----------|--------------------------------------------------------|
| `sovereignty`  | string   | Governing jurisdiction, e.g. `BR`, `EU`, `local-node`. |
| `restrictions` | [string] | Usage restrictions, e.g. `["demo-only"]`.              |
| `sensitivity`  | string   | `public` \| `restricted` \| `sensitive` \| `secret`.   |

### `trust`

| Field      | Type    | Description                                        |
|------------|---------|----------------------------------------------------|
| `verified` | boolean | Whether the asset has been verified.               |
| `score`    | number  | Trust score in [0,1]. **MVP: informational only** — not scientifically validated. |

### `runtime`

`{cpu?, memory?, gpu?}` — requirements for compute assets.

### `interface`

`{type?, version?}` — e.g. `{type: "geomcp-skill", version: "1.0"}`.

### `rendering`

`{min?, max?, colormap?, opacity?}` — visualisation hints.

### `composition`

*Added in 1.0 (additive).* `{components: [...], template?}` — what a composite
asset is **made of**, as references only.

| Field       | Type    | Notes                                                       |
|-------------|---------|-------------------------------------------------------------|
| `id`        | string  | GeoCard id of the referenced asset. **Required**, non-empty.|
| `role`      | string  | `data` \| `model` \| `skill` \| `agent` \| `workflow` \| `knowledge` \| `compute`. Default `data`. |
| `step`      | string  | Step id in the recipe that consumes or produces it.         |
| `required`  | boolean | `false` when the composite still runs without it. Default `true`. |
| `alias`     | string  | Local name used by the recipe, when it differs from the id. |
| `template`  | string  | Optional report/document template id bound to this composition. |

Two components with the same `(id, role)` are rejected — a duplicate reference
would run the same step twice. The same asset in **different** roles is a
different reference and is allowed.

A card describes a solution; it never contains one. Each referenced card keeps
its own identity, access, licence and compliance, and the `id` is a *pointer*,
so reusing a case copies no data. **The execution DAG does not live here** — it
lives in the recipe pointed to by `recipe_ref`.

### `params`

*Added in 1.0 (additive).* `{params: [...]}` — the **parameter contract**: which
fields a reuser may change, within what bounds, and which the author pinned.
Without it a forked case is inert.

| Field         | Type   | Notes                                                         |
|---------------|--------|---------------------------------------------------------------|
| `name`        | string | **Required**, non-empty, unique within the card.               |
| `type`        | string | `string` \| `number` \| `integer` \| `boolean` \| `time` \| `bbox` \| `geometry` \| `enum` \| `ref`. Default `string`. |
| `required`    | boolean| Whether a run must supply it. Default `false`.                |
| `default`     | any    | Value used when the reuser supplies nothing.                   |
| `enum`        | array  | Allowed values; a non-empty `enum` implies `type: enum`.       |
| `minimum` / `maximum` | number | Inclusive bounds; `minimum > maximum` is rejected.  |
| `scope`       | string | `reusable` (a reuser may change it) \| `fixed` (must not be changed). Default `reusable`. |
| `target`      | string | Where it lands in the DAG, e.g. `step1.params.year`.           |
| `description` | string | Human-facing explanation.                                      |

`scope: fixed` is how an author publishes a *result* rather than a *method*:
the case stays reproducible because the pinned inputs cannot drift, while
`reusable` parameters carry the degrees of freedom.

### `recipe_ref`

*Added in 1.0 (additive).* `{recipe_id, version?, entrypoint?, data_snapshot?}`
— pointer from the card to the executable recipe behind it.

| Field           | Type             | Notes                                                 |
|-----------------|------------------|-------------------------------------------------------|
| `recipe_id`     | string           | **Required.** `recipe://<ns>/<name>@<semver>`.         |
| `version`       | string           | Semantic version, when not carried in the id.          |
| `entrypoint`    | string           | Step to start from; defaults to the recipe's entry step.|
| `data_snapshot` | object of string | Data versions the solution was authored against, e.g. `{geokg: "2026.09.1"}`. |

`data_snapshot` is what lets a replay distinguish *"the answer changed because
someone re-ran it"* from *"the answer changed because the data moved"*.

Box `card` → `composition` → `params` → `recipe_ref` → recipe (DAG) → run
(task graph) → outputs. The card is a **facade**; it holds no steps and no
files.

## Contract satisfaction

A GeoCard is more than metadata: the `ContractValidator` decides whether a
card **satisfies a request**:

| Check                  | Rule                                                                 |
|------------------------|----------------------------------------------------------------------|
| CRS compatibility      | card CRS equivalent to requested CRS (pyproj, fallback string match) |
| bbox intersection      | card bbox intersects requested bbox (reprojected when CRSs differ)   |
| temporal overlap       | card `[start, end]` overlaps the requested window                    |
| band compatibility     | every requested band is provided by the card                         |
| resolution             | card resolution is at least as fine as requested                     |

Missing declarations produce **warnings** ("cannot verify") rather than
silent success. Semantic similarity / IoU thresholds are **not** implemented
in the MVP — they are documented extension points, not validated science.

## Python SDK

```python
from geonexus.geocard import GeoCardBuilder, ContractValidator, load_geocard

card = (
    GeoCardBuilder(id="sentinel-2-amazon", type="data", name="Sentinel-2 Amazon",
                   description="Sentinel-2 imagery covering Amazon rainforest")
    .spatial(bbox=[-73.9, -15.0, -44.0, 5.0], crs="EPSG:4326", resolution=10)
    .temporal(start="2015-01-01", end="2025-12-31")
    .band("B04", dtype="uint16").band("B08", dtype="uint16")
    .capability("ndvi")
    .build()
)
card.validate()
card.save("geocard.yaml")

loaded = GeoCard.load("geocard.yaml")
result = ContractValidator().check(
    loaded, bbox=[...], crs="EPSG:4326",
    start="2020-01-01", end="2025-01-01", required_bands=["B04", "B08"],
)
print(result.satisfied, result.reasons, result.warnings)
```

A composite case (2 data + 1 skill + 1 report) is built with the same builder —
the card only ever holds *references*:

```python
case = (
    GeoCardBuilder(
        id="geocard.case.sdg-15-3-1-mng-2019",
        type="workflow",
        name="SDG 15.3.1 土地退化评估（蒙古 2019）",
        description="2 数据 + 1 算子 + 1 报告：可复用的整体方案。",
    )
    .spatial(bbox=[87.7, 41.6, 119.9, 52.1], crs="EPSG:4326")
    .temporal(start="2019-01-01", end="2019-12-31")
    .component("geocard.packed.menggu", role="data", step="s1")
    .component("geocard.oge.lc08-l2", role="data", step="s1")
    .component("2026.9.16 SDG15.3.1", role="skill", step="s2")
    .component("report.sdg1531.mng.2019", role="knowledge", step="s3", alias="report")
    .composition_template("tpl.sdg-report")
    .param("year", type="integer", required=True, minimum=2015, maximum=2024)
    .param("region", type="enum", enum=["蒙古", "墨西哥"], default="蒙古")
    .param("baseline", type="integer", scope="fixed", default=2015)
    .recipe("recipe://geonexus/sdg-15-3-1@1.0.0", entrypoint="s1",
            data_snapshot={"geokg": "2026.09.1"})
    .build()
)
case.validate()
print(case.component_ids(), case.reusable_param_names(), case.fixed_param_names())
# ['geocard.packed.menggu', 'geocard.oge.lc08-l2', '2026.9.16 SDG15.3.1',
#  'report.sdg1531.mng.2019'] ['year', 'region'] ['baseline']
```

## Asset types

| Type       | Meaning                                                     |
|------------|-------------------------------------------------------------|
| `data`     | A geospatial dataset (rasters, vectors, point clouds...).   |
| `model`    | A trained geospatial model (segmentation, classification...).|
| `skill`    | A reusable executable capability.                           |
| `agent`    | An orchestration agent (future).                            |
| `workflow` | A composed sequence of capabilities.                        |
| `knowledge`| Curated knowledge about a region/domain.                    |
| `compute`  | A compute service (CPU/GPU) offered by a node.              |
