"""Collect a read-only inventory of a Salesforce org: what is licensed, configured, built and used.

Everything here is a read (SOQL / Tooling / REST GET). Nothing is deployed or modified.

Usage:
    python -m rc.org_inventory <target-org alias|username> <out_dir>
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .sfcli import SalesforceRest, SfError, display_org

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# (key, SOQL, tooling?) - metadata counted as unmanaged / managed with API version spread where available.
METADATA_QUERIES = [
    ("ApexClass", "SELECT Name, NamespacePrefix, ApiVersion, Status FROM ApexClass", False),
    ("ApexTrigger", "SELECT Name, NamespacePrefix, ApiVersion, Status, TableEnumOrId FROM ApexTrigger", False),
    ("ApexPage", "SELECT Name, NamespacePrefix, ApiVersion FROM ApexPage", False),
    ("ApexComponent", "SELECT Name, NamespacePrefix, ApiVersion FROM ApexComponent", False),
    ("AuraDefinitionBundle", "SELECT DeveloperName, NamespacePrefix, ApiVersion FROM AuraDefinitionBundle", True),
    ("LightningComponentBundle", "SELECT DeveloperName, NamespacePrefix, ApiVersion, IsExposed FROM LightningComponentBundle", True),
    ("Flow", "SELECT Definition.DeveloperName, Definition.NamespacePrefix, ProcessType, ApiVersion, Status FROM Flow WHERE Status = 'Active'", True),
    ("WorkflowRule", "SELECT Name, TableEnumOrId, NamespacePrefix FROM WorkflowRule", True),
    ("ValidationRule", "SELECT ValidationName, Active, NamespacePrefix FROM ValidationRule", True),
    ("CustomObject", "SELECT DeveloperName, NamespacePrefix FROM CustomObject", True),
    ("CustomField", "SELECT COUNT() FROM CustomField", True),
    ("StaticResource", "SELECT Name, NamespacePrefix FROM StaticResource", False),
    ("NamedCredential", "SELECT DeveloperName, NamespacePrefix, Endpoint FROM NamedCredential", False),
    ("ExternalCredential", "SELECT DeveloperName, AuthenticationProtocol FROM ExternalCredential", False),
    ("ConnectedApplication", "SELECT Name FROM ConnectedApplication", False),
    ("ExternalClientApplication", "SELECT MasterLabel FROM ExternalClientApplication", True),
    ("AuthProvider", "SELECT DeveloperName, ProviderType FROM AuthProvider", False),
    ("RemoteSiteSetting", "SELECT SiteName, EndpointUrl, IsActive FROM RemoteProxy", True),
    ("CspTrustedSite", "SELECT DeveloperName, EndpointUrl, IsActive FROM CspTrustedSite", True),
    ("Network", "SELECT Name, Status, UrlPathPrefix FROM Network", False),
    ("Site", "SELECT Name, Status, SiteType FROM Site", False),
    ("PermissionSet", "SELECT COUNT() FROM PermissionSet WHERE IsOwnedByProfile = false", False),
    ("Profile", "SELECT COUNT() FROM Profile", False),
    ("CustomPermission", "SELECT DeveloperName, NamespacePrefix FROM CustomPermission", False),
    ("EmailServicesFunction", "SELECT FunctionName, IsActive FROM EmailServicesFunction", False),
    ("CustomNotificationType", "SELECT DeveloperName FROM CustomNotificationType", True),
    ("FlexiPage", "SELECT DeveloperName, Type, NamespacePrefix FROM FlexiPage", True),
    ("Queue", "SELECT DeveloperName FROM Group WHERE Type = 'Queue'", False),
    ("ScheduledJob", "SELECT CronJobDetail.Name, CronJobDetail.JobType, State FROM CronTrigger WHERE State != 'DELETED'", False),
    ("BotDefinition", "SELECT DeveloperName FROM BotDefinition", False),
    ("GenAiPlannerDefinition", "SELECT DeveloperName FROM GenAiPlannerDefinition", True),
    ("GenAiPromptTemplate", "SELECT DeveloperName FROM GenAiPromptTemplate", True),
    ("PlatformEventChannel", "SELECT DeveloperName FROM PlatformEventChannel", True),
    ("Report", "SELECT COUNT() FROM Report", False),
    ("Dashboard", "SELECT COUNT() FROM Dashboard", False),
    ("EmailTemplate", "SELECT COUNT() FROM EmailTemplate", False),
]

# Unmanaged source pulled for code-level matching (field holding the source).
SOURCE_QUERIES = [
    ("classes", "cls", "SELECT Name, Body FROM ApexClass WHERE NamespacePrefix = null", False, "Name", "Body"),
    ("triggers", "trigger", "SELECT Name, Body FROM ApexTrigger WHERE NamespacePrefix = null", False, "Name", "Body"),
    ("pages", "page", "SELECT Name, Markup FROM ApexPage WHERE NamespacePrefix = null", False, "Name", "Markup"),
    ("components", "component", "SELECT Name, Markup FROM ApexComponent WHERE NamespacePrefix = null", False, "Name", "Markup"),
    ("lwc", "", "SELECT LightningComponentBundle.DeveloperName, FilePath, Source FROM LightningComponentResource "
             "WHERE LightningComponentBundle.NamespacePrefix = null", True, "FilePath", "Source"),
    ("aura", "", "SELECT AuraDefinitionBundle.DeveloperName, DefType, Format, Source FROM AuraDefinition "
              "WHERE AuraDefinitionBundle.NamespacePrefix = null", True, None, "Source"),
]


def _summarise(records: list[dict], key: str) -> dict:
    if records and "expr0" in records[0] and len(records[0]) == 1:
        return {"total": records[0]["expr0"]}
    ns = Counter("managed" if (r.get("NamespacePrefix") or (r.get("Definition") or {}).get("NamespacePrefix"))
                 else "unmanaged" for r in records)
    summary = {"total": len(records), "unmanaged": ns.get("unmanaged", 0), "managed": ns.get("managed", 0)}
    versions = [float(r["ApiVersion"]) for r in records if r.get("ApiVersion")]
    if versions:
        summary["api_versions"] = dict(sorted(Counter(f"{v:.1f}" for v in versions).items()))
        summary["min_api_version"] = min(versions)
    if key == "Flow":
        summary["by_process_type"] = dict(Counter(r.get("ProcessType") for r in records))
    if key == "ScheduledJob":
        summary["by_job_type"] = dict(Counter((r.get("CronJobDetail") or {}).get("JobType") for r in records))
    if key in ("Network", "Site", "NamedCredential", "ExternalCredential", "AuthProvider", "ConnectedApplication",
               "ExternalClientApplication", "BotDefinition", "GenAiPlannerDefinition", "PlatformEventChannel"):
        summary["items"] = [{k: v for k, v in r.items() if not isinstance(v, dict)} for r in records][:100]
    if key == "ApexTrigger":
        summary["objects"] = dict(Counter(r.get("TableEnumOrId") for r in records).most_common(50))
    return summary


def _count_query(rest: SalesforceRest, soql: str, tooling: bool, warnings: list) -> list[dict]:
    if "COUNT()" in soql:
        kind = "tooling/query" if tooling else "query"
        try:
            import urllib.parse
            res = rest.get(f"/services/data/v{rest.version}/{kind}?q={urllib.parse.quote(soql)}")
            return [{"expr0": res.get("totalSize", 0)}]
        except SfError as exc:
            warnings.append(str(exc)[:300])
            return []
    return rest.safe_query(soql, tooling=tooling, warnings=warnings)


def collect_source(rest: SalesforceRest, out_dir: Path, warnings: list) -> dict:
    src_root = out_dir / "org_source"
    counts = {}
    for folder, ext, soql, tooling, name_field, body_field in SOURCE_QUERIES:
        target = src_root / folder
        target.mkdir(parents=True, exist_ok=True)
        records = rest.safe_query(soql, tooling=tooling, warnings=warnings)
        if not records and "NamespacePrefix = null" in soql and tooling:
            # Some API versions do not allow filtering on the parent bundle's namespace; managed source is hidden anyway.
            records = rest.safe_query(soql.split(" WHERE ")[0], tooling=tooling, warnings=warnings)
        for r in records:
            body = r.get(body_field) or ""
            if folder == "aura":
                bundle = (r.get("AuraDefinitionBundle") or {}).get("DeveloperName", "unknown")
                name = f"{bundle}/{r.get('DefType', 'DEF').lower()}.{(r.get('Format') or 'txt').lower()}"
            elif folder == "lwc":
                name = re.sub(r"^lwc/", "", r.get(name_field) or "unknown")
            else:
                name = f"{r.get(name_field)}.{ext}"
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        counts[folder] = len(records)
    return counts


def evaluate_capabilities(inv: dict) -> list[dict]:
    """Decide which capabilities the org actually uses, with human-readable evidence."""
    spec = json.loads((CONFIG_DIR / "capabilities.json").read_text(encoding="utf-8"))
    counts = inv["record_counts"]
    objects = set(inv["sobjects"])
    md = inv["metadata"]
    namespaces = {p["namespace"] for p in inv["packages"] if p.get("namespace")}
    licenses = [(l["name"], l.get("used", 0)) for l in inv["licenses"]]
    flow_types = md.get("Flow", {}).get("by_process_type", {})
    results = []
    for cap in spec["capabilities"]:
        det, evidence = cap.get("detect", {}), []
        if det.get("always"):
            evidence.append("core platform")
        for obj in det.get("records", []):
            if counts.get(obj, 0) > 0:
                evidence.append(f"{obj}: {counts[obj]:,} records")
        for obj in det.get("objects", []):
            if obj in objects:
                evidence.append(f"object {obj} available")
        for mtype, minimum in det.get("metadata", {}).items():
            n = md.get(mtype, {}).get("unmanaged", md.get(mtype, {}).get("total", 0))
            if n >= minimum:
                evidence.append(f"{n} {mtype}")
        for ns in det.get("packages", []):
            if ns in namespaces:
                evidence.append(f"package namespace {ns}")
        for lic in det.get("licenses", []):
            for name, used in licenses:
                if lic.lower() in (name or "").lower() and used:
                    evidence.append(f"license {name} ({used} used)")
        for ptype in det.get("flow_types", []):
            if flow_types.get(ptype):
                evidence.append(f"{flow_types[ptype]} active {ptype} flows")
        for lt in det.get("login_types", []):
            for row in inv.get("logins_30d", []):
                if lt.lower() in f"{row.get('LoginType')} {row.get('LoginSubType')}".lower():
                    evidence.append(f"{row.get('count')} logins via {row.get('LoginType')} ({row.get('Application')})")
        results.append({"id": cap["id"], "label": cap["label"], "present": bool(evidence),
                        "strength": "strong" if len(evidence) >= 2 or det.get("always") else ("weak" if evidence else "none"),
                        "evidence": evidence[:8]})
    return results


def collect(target: str | dict, out_dir: Path, include_source: bool = True, log=print) -> dict:
    """`target` is an sf CLI alias/username, or an org dict (instanceUrl, accessToken, apiVersion, username,
    alias, optional refresh) from the web login. `log` receives one-line progress messages."""
    out_dir.mkdir(parents=True, exist_ok=True)
    org = display_org(target) if isinstance(target, str) else target
    target = org.get("alias") or org.get("username") if not isinstance(target, str) else target
    rest = SalesforceRest(org)
    warnings: list[str] = []
    log(f"  Connected to {org['instanceUrl']} (API v{rest.version}) as {org['username']}")

    org_rows = rest.safe_query(
        "SELECT Id, Name, OrganizationType, IsSandbox, InstanceName, LanguageLocaleKey, TimeZoneSidKey, "
        "NamespacePrefix, TrialExpirationDate, CreatedDate FROM Organization", warnings=warnings)
    log("  - record counts and object list")
    try:
        record_counts = {r["name"]: r["count"] for r in rest.get(f"/services/data/v{rest.version}/limits/recordCount")["sObjects"]}
    except SfError as exc:
        warnings.append(str(exc)[:300])
        record_counts = {}
    sobjects = [s["name"] for s in rest.get(f"/services/data/v{rest.version}/sobjects")["sobjects"]]

    log("  - packages and licenses")
    pkgs = rest.safe_query("SELECT SubscriberPackage.NamespacePrefix, SubscriberPackage.Name, "
                           "SubscriberPackageVersion.Name, SubscriberPackageVersion.MajorVersion, "
                           "SubscriberPackageVersion.MinorVersion FROM InstalledSubscriberPackage",
                           tooling=True, warnings=warnings)
    packages = [{"namespace": (p.get("SubscriberPackage") or {}).get("NamespacePrefix"),
                 "name": (p.get("SubscriberPackage") or {}).get("Name"),
                 "version": "{}.{}".format((p.get("SubscriberPackageVersion") or {}).get("MajorVersion"),
                                           (p.get("SubscriberPackageVersion") or {}).get("MinorVersion"))}
                for p in pkgs]
    licenses = [{"name": r["Name"], "total": r["TotalLicenses"], "used": r["UsedLicenses"], "kind": "user"}
                for r in rest.safe_query("SELECT Name, TotalLicenses, UsedLicenses, Status FROM UserLicense "
                                         "WHERE Status = 'Active'", warnings=warnings)]
    licenses += [{"name": r["MasterLabel"], "total": r["TotalLicenses"], "used": r["UsedLicenses"], "kind": "permset"}
                 for r in rest.safe_query("SELECT MasterLabel, TotalLicenses, UsedLicenses FROM PermissionSetLicense "
                                          "WHERE Status = 'Active'", warnings=warnings)]
    users = rest.safe_query("SELECT Profile.UserLicense.Name lic, COUNT(Id) n FROM User WHERE IsActive = true "
                            "GROUP BY Profile.UserLicense.Name", warnings=warnings)

    log("  - metadata counts and API versions")
    metadata = {}
    for key, soql, tooling in METADATA_QUERIES:
        metadata[key] = _summarise(_count_query(rest, soql, tooling, warnings), key)

    log("  - login / integration patterns (last 30 days)")
    # Aggregated client-side: LoginHistory GROUP BY support varies, and sub-type / API version are what identify
    # OAuth flows and old-API SOAP login() callers.
    raw = rest.safe_query("SELECT LoginType, LoginSubType, Application, ApiType, ApiVersion FROM LoginHistory "
                          "WHERE LoginTime = LAST_N_DAYS:30 ORDER BY LoginTime DESC", warnings=warnings,
                          limit_records=50000)
    grouped = Counter((r.get("LoginType"), r.get("LoginSubType"), r.get("Application"), r.get("ApiType"),
                       r.get("ApiVersion")) for r in raw)
    logins = [{"LoginType": k[0], "LoginSubType": k[1], "Application": k[2], "ApiType": k[3], "ApiVersion": k[4],
               "count": n} for k, n in grouped.most_common()]

    source_counts = {}
    if include_source:
        log("  - unmanaged source code (Apex, Visualforce, LWC, Aura)")
        source_counts = collect_source(rest, out_dir, warnings)

    inv = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_org": target,
        "username": org.get("username"),
        "instance_url": org.get("instanceUrl"),
        "api_version": rest.version,
        "organization": org_rows[0] if org_rows else {},
        "record_counts": record_counts,
        "sobjects": sobjects,
        "packages": packages,
        "licenses": licenses,
        "active_users_by_license": {u.get("lic"): u.get("n") for u in users},
        "metadata": metadata,
        "logins_30d": logins,
        "source_counts": source_counts,
        "warnings": warnings,
    }
    inv["capabilities"] = evaluate_capabilities(inv)
    (out_dir / "org_inventory.json").write_text(json.dumps(inv, indent=1, ensure_ascii=False, default=str),
                                                encoding="utf-8")
    present = [c["label"] for c in inv["capabilities"] if c["present"]]
    log(f"  Inventory saved. {len(present)} capabilities in use; {len(warnings)} queries skipped (permissions/edition).")
    return inv


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    collect(sys.argv[1], Path(sys.argv[2]))
