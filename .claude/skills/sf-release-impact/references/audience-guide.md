# Writing for each audience

The documentation is technical, but it is read by five roles. Each field has a primary reader - write it for them.

| Field / section | Primary reader | Style |
|---|---|---|
| `summary` (per feature) | PM, PO, Managers | 1-2 sentences, plain language: what changes and who notices. No API names unless unavoidable. |
| `rationale` | Architects, Developers | Why this rating for *this* org, citing evidence (component names, counts, login types). |
| `actions` | Developers, Admins | Imperative, verifiable steps: "Replace `URL.getSalesforceBaseUrl()` in `LoginSvc.cls` with `URL.getOrgDomainUrl()`", "In Setup > Session Settings add ...". Include the SOQL or Setup path to verify. |
| `test_notes` | QA, Developers | What to regression-test in the preview sandbox. |
| `deadline` | Release Manager, PM | Concrete: "Before Winter '27 production upgrade", "Before Spring '27 enforcement", or "n/a". |

## Synthesis sections
- **Managers** - risk level, how many items need action, effort order of magnitude, decisions/approvals needed. No jargon.
- **PM / PO** - user-facing changes (what end users and portal users will see), training/communication needs,
  opportunities worth adding to the backlog (new features relevant to the org's business processes).
- **Architects** - integration and identity impacts, security posture, platform limits, API version strategy,
  technical-debt signals (old API versions, legacy automation), cross-cutting design decisions.
- **Developers** - concrete code/config task list grouped by component type (Apex, LWC/Aura, Visualforce, Flow,
  integrations), with file/component names and the features they come from.

## Tone
- Factual, specific, short sentences. Prefer numbers and names over adjectives.
- Never copy release-note marketing phrases ("supercharge", "seamlessly").
- If something is an assumption, say so.
