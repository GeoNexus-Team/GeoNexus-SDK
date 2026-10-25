# SDK 1.1.1.dev20261015 — source publication

This publishes the already-built local development snapshot, including its
previously uncommitted contract, transport, and registration work. The runtime
package files are preserved byte-for-byte; this publication adds release
documentation, generated-file exclusions, and the full test dependency extras
to CI. It does not publish to PyPI or change the version identifier.

## Included integration interfaces

- `geonexus.geocard.contracts`: request validation, findings/reports, runtime
  observations, and result manifests.
- `create_mcp_server(server, *, http_guarded=False)`: official MCP integration.
  Only set `http_guarded=True` when the enclosing HTTP application enforces
  authentication; this option is not itself an authentication mechanism.
- `geonexus.registration` / `georegister`: metadata import, draft creation,
  profile validation, endpoint probing, and registry submission.
- GeoCard, ServiceBinding and ServiceState schemas, registry submission/review
  boundaries, MCP client compatibility, documentation and regression tests.

The lightweight base install retains Pydantic, JSON Schema and YAML. Optional
HTTP, server, MCP and GIS dependencies are installed through extras. Use
`pip install '.[dev,all]'` for the complete development test suite.

## Reproducibility and limitations

The reference pre-publication wheel is
`geonexus_sdk-1.1.1.dev20261015-py3-none-any.whl`, SHA256
`566fedfd776f27939a010215c483cb1e943109e17c18ff0244c6b5b8acb279da`.
All 112 files under its `geonexus/` package match the published source snapshot.
A rebuilt wheel may have a different archive hash because documentation,
metadata and ZIP timestamps are different; compare the package contents.

This is a development snapshot. Local pre-publication checks identified seven
lint findings, 47 files requiring formatting, and 40 mypy errors in the
available validation environment. These gates are retained in CI, not disabled
or described as passing. Broad formatting/type cleanup is separate from
publishing the runtime snapshot. Functional validation on Python 3.12 passed all 903 tests (zero failures,
errors or skips). Documentation consistency and wheel construction also passed.
The rebuilt wheel retains all 112 reference runtime package files byte-for-byte.

Generated case outputs, private runtime directories, credentials, virtual
environments, and wheel binaries are not part of this source publication.
