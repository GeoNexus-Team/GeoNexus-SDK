# GeoNexus v0.2 contracts (additive SDK change)

GeoNexus v0.2 is the platform milestone; the existing GeoCard 1.0 vocabulary and SDK package version are retained.

- `geonexus.geocard.GeoCard` remains the single card model. Optional `knowledge_refs` contains GeoKG entity IDs; empty refs are omitted by `to_dict()` for legacy compatibility.
- Both repository and wheel JSON Schemas now include the existing composition, parameter and recipe fields, plus knowledge refs. The wheel copy previously lacked the first three fields.
- `geonexus.geocard.contracts` provides `ContractRequest`, deterministic findings/reports, `RuntimeObservation`, and `ResultManifest`/`ResultArtifact`. None imports Platform, GeoKG, LLM orchestration or a database.
- Runtime reachability, execution and result records are separate from static GeoCard semantics. Existing legacy runtime metadata is retained for compatibility; new observations use separate envelopes.
- CRS/extent/time/bands/resolution use the existing canonical validator. Extent/time mean **overlap**, not full coverage. Units are exact declarations; there is no implicit unit conversion. Unknown metadata or unresolved policies prevent an executable plan.
- ISO timestamps with offsets normalize to UTC when compared to dates or timezone-free metadata. Requests with mixed timezone conventions are rejected.
- Sovereignty must equal the requested execution jurisdiction. `no-export` blocks raw export; other restrictions or non-public sensitivity require a separate authorization decision and remain unknown here.
- Result manifests carry artifact references, media types, SHA-256, producing node, method and synthetic flag; never an embedded raster.

The official MCP adapter supports `mcp>=1.26,<3`: official 1.26 FastMCP and 2.3 MCPServer are selected by available API, without another transport implementation. The five existing tools (`geo_capabilities`, `geo_describe`, `geo_execute`, `geo_plan`, `geo_health`) and legacy GeoMCP routes remain.

## Verification

From the SDK root, in a Python environment with the project and dev/MCP dependencies:

```sh
PYTHONPATH=src coverage run -m pytest
coverage report
PYTHONPATH=src pytest tests/test_v02_contracts.py tests/test_mcp_adapter.py
```

Run the MCP adapter tests once with official 1.26 and once with official 2.3 in separate environments. Core changes do not require a GeoKG service. Existing full-repository lint/format/type debt is reported separately in the review bundle; these checks must not be described as green.
