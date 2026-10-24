# CSM `find_entitlements` observation-contract audit

## Finding

The pinned CSM MCP image exposes `find_entitlements` with an output schema that declares `product_id` as an integer. The same field description says `null` means the entitlement applies to all products. The endpoint delegates to `CSMManager.find_entitlements`; bytecode inspection of the pinned image shows that the manager builds optional filters and issues a `SELECT` over the entitlement table. The tool is therefore read-only for this exact image and tool-schema digest.

The public first-wave ledger retained a `ValidationError` for this tool, but intentionally did not retain its raw response. The historical failure therefore cannot be attributed conclusively to `product_id: null`. This change fixes the documented nullable-schema mismatch and provides a deterministic regression; a new isolated evaluation must confirm whether it resolves the live failure.

## Scoped compatibility

- Image: `shivakrishnareddyma225/enterpriseops-gym-mcp-csm@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189`
- Tool: `find_entitlements`
- Original tool-schema SHA-256: `cf9941b75174a6cb699916957f63b6fb566a83b520bc173d441e869f3586ff9c`
- Field: `product_id`, documented as nullable when the entitlement applies to all products
- Observation contract: `enterprise-mcp-observation-v3`

Only the evaluator's typed observation schema accepts `integer | null`. The original upstream schema and response remain intact in the observation record. Other tools, images, schema revisions, and contract versions receive no compatibility override. Validation remains enabled against the scoped corrected schema.

## Evidence and limits

The schema was read from a fresh, isolated CSM environment without calling a business tool. The pinned image was inspected locally; its `find_entitlements` handler delegates to a database method whose bytecode contains a parameterized `SELECT` and no write statement. The deterministic regression feeds a synthetic response with `product_id: null`; it is not a model run or an official score. No historical unknown route was replayed, and no Jev or Qwen request was made.
