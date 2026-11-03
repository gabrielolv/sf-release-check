"""Read-only SOQL for the AI agent when the sf CLI is not installed (Docker / web app runs).

    python -m rc.soql "SELECT Id, Name FROM ApexClass LIMIT 10" [--tooling]

Connection comes from SF_INSTANCE_URL, SF_ACCESS_TOKEN and SF_API_VERSION, which the web app passes only to the
Claude Code processes of a run. Only a single SELECT statement is accepted; at most 200 rows are printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .sfcli import SalesforceRest, SfError


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("soql")
    ap.add_argument("--tooling", action="store_true", help="query the Tooling API")
    args = ap.parse_args()
    query = args.soql.strip().rstrip(";")
    if not re.match(r"(?is)^select\s", query) or ";" in query:
        print("ERROR: only a single SELECT statement is allowed.")
        return 2
    try:
        rest = SalesforceRest({"instanceUrl": os.environ["SF_INSTANCE_URL"], "accessToken": os.environ["SF_ACCESS_TOKEN"],
                               "apiVersion": os.environ.get("SF_API_VERSION")})
    except KeyError:
        print("ERROR: no org connection available.")
        return 2
    try:
        rows = rest.query(query, tooling=args.tooling, limit_records=200)[:200]
    except SfError as exc:
        print(f"ERROR: {str(exc)[:500]}")
        return 1
    print(json.dumps({"rows": len(rows), "records": rows}, indent=1, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
