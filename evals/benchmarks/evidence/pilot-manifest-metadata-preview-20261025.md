# Pilot manifest metadata-only preview

**Status: preview only; the pilot is not frozen or authorized to run.**

The existing selector deterministically produced the approved sample sizes and route denominator using the frozen v11 manifest and seed `sop-pilot-20261024-v1`: 10 CSM, 10 ITSM, 2 τ retail test, 2 τ telecom test, and 68 total route slots. Its preview digest is `00912e636ad0d52738ebf942e80816277cbb9f0319dc27ee52a7322948769ea5`; the source manifest digest is `f7fed533cf54c46bf5fdc26e6c84fad11404d3c50f4fa88dbf356db725185ea0`.

The preview used only the manifest's benchmark, domain, split, exposure, task ID, route-profile, resource-profile, response-policy, and status metadata. It did not print or inspect task prompts, target states, reference actions, or scoring materials. No task IDs were copied to this evidence file. No runtime manifest was written, and there were zero model, business MCP, or official scorer calls.

The preview is not a pilot freeze: `diagnostic_evidence` is null, the current manifest command requires a passing bounded diagnostic, and Fireworks provider-matched pilot eligibility still needs implementation. The selected sample must be frozen against the final candidate, profile, route, and budget after those gates pass. This preview does not authorize live work.

The existing manifest builder tests passed under the project's `Agent311` environment: 10 passed. The exact machine summary is in [pilot-manifest-metadata-preview-20261025.json](pilot-manifest-metadata-preview-20261025.json).
