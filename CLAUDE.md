# Salesforce Release Impact Check

Python pipeline + Claude agent that rates the impact of a Salesforce release on a connected org. See README.md.

- Entry points: web app `webapp/app.py` (Docker: `docker compose up`, port 1717 is fixed by the Salesforce OAuth
  callback) and CLI `release_check.py` (wrappers `run.ps1`, `run.sh`). Pipeline modules in `scripts/rc/`.
- AI engines: `rc/ai_api.py` (Claude API key, tool runner) and `rc/ai_analysis.py` (headless Claude Code - used by
  the CLI and by the web app's "Claude account" option via `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`).
  Both read the same rules from `.claude/skills/sf-release-impact/references/`. Without the sf CLI (Docker) the agent
  uses `python -m rc.soql` for read-only SOQL. Never implement a custom claude.ai OAuth login.
- Skill `/sf-release-impact` (`.claude/skills/sf-release-impact/`) - how to run it and answer questions about a run;
  its `references/` are the rating rubric, audience guide and JSON contracts.
- Agent `sf-release-impact-analyst` (`.claude/agents/`) - invoked headless by `scripts/rc/ai_analysis.py`.
- Distribution: public repo github.com/gabrielolv/sf-release-check; `.github/workflows/docker-publish.yml` publishes
  `ghcr.io/gabrielolv/sf-release-check` (latest on main, semver on `v*` tags). `docker/entrypoint.sh` trusts company
  root CAs mounted at `/certs` (Zscaler). Never commit `data/`, `output/`, `.env`, `certs/*.crt` or release-note files.
- Run artefacts live in `output/<run>/`; `report/report_data.json` is the easiest file to answer questions from.
- Org access is read-only. Never run sf commands that deploy, create, update or delete.
- Changing a JSON contract means updating `references/output-contracts.md`, `ai_analysis.py` and `report.py` together.
