# Impact rubric (single source of truth)

Impact = **how much this specific org must do or risks, because of this change**, not how big the feature is in general.
Decide in this order: applicability -> severity of the change -> exposure in this org.

## Not Applicable
The change cannot affect this org:
- the product / cloud / license is not present (no evidence in the org profile), or
- the feature is limited to editions the org does not have, or
- it is a pilot the org is not enrolled in, a canceled release update, or a change for brand-new orgs only
  ("in new orgs" / "orgs created after") when this org already exists.

## Low
The org uses the area, but nothing breaks and nothing is required:
- new optional features, beta features, or UI enhancements that must be switched on ("From Setup ... enable")
- cosmetic or additive changes (new fields, new components, new API resources) with no behavior change for existing usage
- **Action:** none required; optionally evaluate as an opportunity. Effort `None`.

## Medium
Existing behavior in the org changes, or work is required, but there is no outage risk if handled in normal cadence:
- changes that are **on by default / automatic** and visible to users (UX, layout, defaults, accessibility enforcement)
- release updates on used capabilities that are **scheduled for a later release** (prepare now, enforce later)
- deprecations with a future date; API-version-bound behavior changes that hit only when components are upgraded
- new limits, permission or security tightening where the org is plausibly but not demonstrably affected
- **Action:** test in the preview sandbox, communicate to users/admins, or plan a backlog item. Effort `S` or `M`.

## High
A breaking, mandatory, or security-relevant change that hits something the org **demonstrably uses**, with a hard date:
- retirements / removals / blocks of a feature, API version, auth flow or component the org uses
  (e.g. OAuth username-password flow with active integration logins, SOAP `login()` from old API versions,
  code that references a removed method, Visualforce/Aura behavior the org's pages rely on)
- release updates **enforced with this release** (or before the next one) on capabilities the org uses
- changes that require code or configuration changes before the production upgrade to avoid failures,
  data exposure, or users locked out
- **Action:** must be addressed before the production upgrade date; name owner and deadline. Effort `S`/`M`/`L`.

## Tie-breakers
- Code evidence (a hit in `org/org_source/`) beats keyword matches. No evidence of usage -> at most Medium.
- Uncertain usage of a breaking change -> rate High with `confidence: "low"` and a verification action, never silently Low.
- Security/identity changes affecting external users (Experience Cloud guest/portal) rate one level higher than the
  same change for internal users only.
- Several features describing the same change (summary + detail): rate them consistently.

## Fields
- `confidence`: `high` (code/config evidence confirms usage), `medium` (capability evidence), `low` (inferred).
- `effort`: `None` (no work), `S` (< 1 day), `M` (1-5 days), `L` (> 5 days or cross-team).
- `owner_roles`: any of `Developer`, `Admin`, `Architect`, `Integration Owner`, `Security`, `PO`, `QA`, `Release Manager`.
