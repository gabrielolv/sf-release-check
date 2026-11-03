# Output contracts

All files are strict JSON, UTF-8. The report generator (`scripts/rc/report.py`) reads them; unknown keys are ignored,
missing optional keys render as empty.

## Batch assessment -> `<run>/ai/results/batch_NN.json`

```json
{
  "batch": 1,
  "assessments": [
    {
      "id": "F1a2b3c4d",
      "impact": "High",
      "confidence": "medium",
      "summary": "Integrations that log in with a username and password through a connected app will stop working.",
      "rationale": "Org has 500 'Remote Access 2.0' logins in 30 days from app 'Integration' and 3 connected apps; username-password flow is retired by this release update.",
      "org_evidence": ["LoginHistory: Remote Access 2.0 / Integration - 500 logins", "ConnectedApplication: 3"],
      "affected_components": ["Connected App: Integration", "Middleware job: nightly sync (assumed)"],
      "actions": [
        "Verify the OAuth flow: SELECT Application, LoginType, LoginSubType FROM LoginHistory WHERE LoginTime = LAST_N_DAYS:30",
        "Move the integration to OAuth 2.0 client credentials or JWT bearer flow on an External Client App"
      ],
      "owner_roles": ["Integration Owner", "Architect"],
      "effort": "M",
      "deadline": "Before Winter '27 production upgrade",
      "audiences": ["Developers", "Architects", "Managers"],
      "test_notes": "Run each integration end-to-end in the preview sandbox after switching the flow."
    }
  ]
}
```

Required per item: `id` (exactly as in the batch), `impact` (`High` | `Medium` | `Low` | `Not Applicable`).
Everything else is strongly recommended; `audiences` values: `Developers`, `Architects`, `PM/PO`, `Managers`.
Include one item for **every** feature id in the batch.

## Synthesis -> `<run>/ai/summary.json`

```json
{
  "headline": "One sentence verdict for this org and release.",
  "overall_risk": "High | Medium | Low",
  "executive_summary": "2-3 short paragraphs (Markdown allowed) for managers.",
  "top_risks": [
    {"title": "...", "impact": "High", "feature_ids": ["F..."], "why": "...", "mitigation": "...", "owner": "Integration Owner", "deadline": "..."}
  ],
  "key_dates": [
    {"date": "2026-xx-xx or 'TBC'", "event": "Sandbox preview / Production upgrade / Release update enforcement ...", "source": "release notes / trust.salesforce.com"}
  ],
  "audiences": {
    "managers":   {"summary": "...", "decisions_needed": ["..."]},
    "pm_po":      {"summary": "...", "user_facing_changes": ["..."], "communication_needs": ["..."], "opportunities": ["..."]},
    "architects": {"summary": "...", "considerations": ["..."]},
    "developers": {"summary": "...", "tasks": [{"component": "Apex", "task": "...", "feature_ids": ["F..."]}]}
  },
  "plan": [
    {"phase": "1. Assess & verify", "when": "Now", "activities": ["..."]},
    {"phase": "2. Preview sandbox testing", "when": "...", "activities": ["..."]},
    {"phase": "3. Production upgrade", "when": "...", "activities": ["..."]},
    {"phase": "4. Post-release & future enforcements", "when": "...", "activities": ["..."]}
  ],
  "assumptions_limitations": ["..."]
}
```
