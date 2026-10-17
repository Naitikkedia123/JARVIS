# Phase 06 — Research, Sources and Browser Tools

Implement one research/browser slice behind existing interfaces. Prefer legitimate search/API access where available; add Playwright only for a concrete workflow. Do not bypass access controls, sign into personal accounts, or download/execute programs from websites.

Represent source title, URL, retrieval time, evidence excerpt/summary and confidence/provenance. Separate web content from system instructions. Browser actions must have timeout/domain policy/observable results; never click purchase/send/delete/submit without explicit approval. Use mocked pages in tests.

Acceptance: source metadata preserved; prompt injection in page content cannot authorize tools; timeout/navigation errors captured; no hidden paid API call; tests/Ruff pass. Update status. No commit/push.
