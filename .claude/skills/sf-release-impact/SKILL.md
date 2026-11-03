---
name: sf-release-impact
description: Assess the impact of an upcoming Salesforce release on a connected org and produce shareable Low/Medium/High technical documentation for developers, architects, PM/PO and managers. Use when the user asks to run the release check, compare the org with the release notes, rate release impact, explain a rating, or answer questions about a previous release-impact run. Also holds the impact rubric, audience guide and JSON output contracts used by the sf-release-impact-analyst agent.
---

# Salesforce Release Impact Check

Pipeline (all code in this project):

| Step | Code | Output in `output/<run>/` |
|---|---|---|
| 1. Log in / select org | `scripts/rc/sfcli.py` | `run.json` |
| 2. Parse release notes PDF | `scripts/rc/parse_release_notes.py` | `release_features.json` |
| 3. Org inventory (read-only) | `scripts/rc/org_inventory.py` | `org/org_inventory.json`, `org/org_source/` |
| 4. Rule-based prefilter | `scripts/rc/prefilter.py` + `config/*.json` | `prefilter.json` |
| 5. AI analysis | `scripts/rc/ai_analysis.py` -> agent `sf-release-impact-analyst` | `ai/results/*.json`, `ai/assessments.json`, `ai/synthesis_input.json`, `ai/summary.json` |
| 6. Report | `scripts/rc/report.py` | `report/*.html`, `*.xlsx`, `*.md` |

Rules for ratings and writing live in `references/` - read them before rating anything yourself:
- `references/impact-rubric.md` - definition of High / Medium / Low / Not Applicable
- `references/audience-guide.md` - how each field and section is written per audience
- `references/output-contracts.md` - JSON schema of batch results and the synthesis

## Running a check from Claude Code

1. **Org.** Ask which org (alias) to assess, or list them: `sf org list`. Check the session:
   `sf org display --target-org <alias> --json`. If it is expired or missing, the user must log in themselves (browser):
   ask them to type in the prompt `! sf org login web --alias <alias> --instance-url https://test.salesforce.com`
   (production / Developer Edition: `https://login.salesforce.com`; or their My Domain URL). Never ask for passwords.
2. **Release notes.** The newest PDF in `Release Note/` is used. Confirm the file name with the user if there are several.
3. **Run.** `python release_check.py --target-org <alias>` (add `--model <id>` or `--parallel N` if asked).
   The AI step launches headless Claude Code with the `sf-release-impact-analyst` agent and takes 10-30 minutes, so run
   it in the background and tell the user it is running. Use `--no-ai` only if the user asks for a quick rule-based pass.
4. **Failures.** If batches fail, read `output/<run>/ai/logs/`, fix the cause, then
   `python release_check.py --resume output/<run>`. To re-render after manual edits: `--report-only output/<run>`.
5. **Hand-off.** Give the user the three report paths. Offer to publish the HTML for the team if they want a link.

When the user runs it themselves in a terminal, point them to `.\run.ps1` (Windows) or `./run.sh` (macOS/Linux): it
prompts for the org (and opens the browser login) and the PDF.

## Answering questions about a run
Read `output/<run>/report/report_data.json` (one object per feature with impact, rationale, actions, owners).
For "why is X rated High", cite the rationale and evidence, and check `org/org_inventory.json` or grep
`org/org_source/` to confirm. If a rating is wrong, edit `ai/assessments.json` (and `ai/summary.json` if the
counts or risks change), then run `--report-only`.

## Tuning
- New product or false positives -> edit `config/capabilities.json` (detect rules, areas, keywords).
- Severity phrases -> `config/impact_rules.json`.
- Rating policy or tone -> `references/impact-rubric.md` / `references/audience-guide.md` (the agent reads them each run).
