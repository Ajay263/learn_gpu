"""Deploy the `news-ingestion` NiFi flow through the NiFi REST API (flow as code).

    python infra/nifi/deploy_flow.py            # create if absent
    python infra/nifi/deploy_flow.py --replace  # stop, delete and recreate (drops queued FlowFiles!)
    python infra/nifi/deploy_flow.py --relist   # clear ListS3 state so files left in landing are re-ingested

Reads credentials from the repo's .env. Secrets go into a Parameter Context as *sensitive*
parameters (encrypted at rest by NiFi with NIFI_SENSITIVE_PROPS_KEY); processors only reference them.

Flow:
  ListS3(landing/uploads/) → CheckSize → FetchS3Object → IdentifyMimeType → IsVideo → SHA-256
    → ComputeKeys → GetS3ObjectMetadata(bronze key)
         found     → MarkDuplicate ─┐
         not found → PutOriginal  ──┴→ ManifestKey → ToJSON → PutManifest → NotifyAPI → DeleteLanding
  Rejections (size, type, API 4xx) → RejectInfo → CopyToRejected → RejectJSON → PutRejectManifest → DeleteRejected
  Transient failures (S3, API 5xx/unreachable) loop with penalties; after the retry budget the file
  stays in landing (re-ingest with --relist).
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PG_NAME = "news-ingestion"
CTX_NAME = "news-ingestion-params"
AWS = "org.apache.nifi.processors.aws.s3."
STD = "org.apache.nifi.processors.standard."
BUNDLES = {
    "aws": {"group": "org.apache.nifi", "artifact": "nifi-aws-nar", "version": "2.12.0"},
    "std": {"group": "org.apache.nifi", "artifact": "nifi-standard-nar", "version": "2.12.0"},
    "upd": {"group": "org.apache.nifi", "artifact": "nifi-update-attribute-nar", "version": "2.12.0"},
}
TS = "${now():format(\"yyyyMMdd'T'HHmmss'Z'\", 'GMT')}"


def load_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


class Nifi:
    def __init__(self, base: str, user: str, password: str):
        self.http = httpx.Client(base_url=base, verify=False, timeout=60)  # self-signed cert, loopback only
        tok = self.http.post("/access/token", data={"username": user, "password": password})
        tok.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {tok.text}"

    def req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else {}

    def root_id(self) -> str:
        return self.req("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]

    def find_pg(self, parent: str, name: str) -> dict | None:
        for pg in self.req("GET", f"/process-groups/{parent}/process-groups")["processGroups"]:
            if pg["component"]["name"] == name:
                return pg
        return None

    def find_ctx(self, name: str) -> dict | None:
        for c in self.req("GET", "/flow/parameter-contexts")["parameterContexts"]:
            if c["component"]["name"] == name:
                return c
        return None

    def run_status(self, pg_id: str, state: str) -> None:
        self.req("PUT", f"/flow/process-groups/{pg_id}", json={"id": pg_id, "state": state})

    def services_state(self, pg_id: str, state: str) -> None:
        self.req("PUT", f"/flow/process-groups/{pg_id}/controller-services", json={"id": pg_id, "state": state})


def ensure_param_context(nifi: Nifi, env: dict[str, str]) -> str:
    params = {
        "minio_endpoint": ("http://minio:9000", False),
        "minio_access_key": (env["MINIO_NIFI_ACCESS_KEY"], True),  # NiFi marks Access Key ID sensitive
        "minio_secret_key": (env["MINIO_NIFI_SECRET_KEY"], True),
        "bucket_landing": ("landing", False),
        "bucket_bronze": ("bronze", False),
        "bucket_manifests": ("manifests", False),
        "max_upload_bytes": (env.get("MAX_UPLOAD_BYTES", "1073741824"), False),
        "api_ingest_url": (env.get("NIFI_API_INGEST_URL", "http://api:8000/internal/ingestions"), False),
        "ingest_auth_header": (f"Bearer {env['INGEST_SERVICE_TOKEN']}", True),
    }
    body_params = [{"parameter": {"name": k, "value": v, "sensitive": s}} for k, (v, s) in params.items()]
    existing = nifi.find_ctx(CTX_NAME)
    if existing:
        # Updates go through an async update request (NiFi restarts referencing components itself).
        cid = existing["id"]
        body = {"revision": existing["revision"], "id": cid, "component": {"id": cid, "parameters": body_params}}
        upd = nifi.req("POST", f"/parameter-contexts/{cid}/update-requests", json=body)
        rid = upd["request"]["requestId"]
        for _ in range(60):
            st = nifi.req("GET", f"/parameter-contexts/{cid}/update-requests/{rid}")["request"]
            if st["complete"]:
                break
            time.sleep(1)
        nifi.req("DELETE", f"/parameter-contexts/{cid}/update-requests/{rid}")
        return cid
    ctx = nifi.req(
        "POST",
        "/parameter-contexts",
        json={"revision": {"version": 0}, "component": {"name": CTX_NAME, "parameters": body_params}},
    )
    return ctx["id"]


def create_pg(nifi: Nifi, parent: str, ctx_id: str) -> str:
    pg = nifi.req(
        "POST",
        f"/process-groups/{parent}/process-groups",
        json={
            "revision": {"version": 0},
            "component": {
                "name": PG_NAME,
                "position": {"x": 0, "y": 0},
                "parameterContext": {"id": ctx_id},
                "comments": "Validate uploads, checksum, dedupe, store originals, register with API. "
                "Deployed by infra/nifi/deploy_flow.py — edit the script, not the canvas.",
            },
        },
    )
    return pg["id"]


def create_credentials(nifi: Nifi, pg_id: str) -> str:
    cs = nifi.req(
        "POST",
        f"/process-groups/{pg_id}/controller-services",
        json={
            "revision": {"version": 0},
            "component": {
                "type": "org.apache.nifi.processors.aws.credentials.provider.service."
                "AWSCredentialsProviderControllerService",
                "bundle": BUNDLES["aws"],
                "name": "minio-nifi-credentials",
            },
        },
    )
    names = {d["displayName"]: n for n, d in cs["component"]["descriptors"].items()}
    props = {names["Access Key ID"]: "#{minio_access_key}", names["Secret Access Key"]: "#{minio_secret_key}"}
    nifi.req(
        "PUT",
        f"/controller-services/{cs['id']}",
        json={"revision": cs["revision"], "component": {"id": cs["id"], "properties": props}},
    )
    return cs["id"]


def s3_props(creds: str, **extra: str) -> dict[str, str]:
    return {
        "Region": "us-east-1",
        "AWS Credentials Provider Service": creds,
        "Endpoint Override URL": "#{minio_endpoint}",
        "Use Path Style Access": "true",
        **extra,
    }


def processor_specs(creds: str) -> list[dict]:
    """Declarative flow definition. (name, type, bundle, properties, auto-terminated relationships, extras)."""
    retry_penalty = {"penaltyDuration": "30 sec"}
    return [
        dict(
            name="ListLanding",
            type=AWS + "ListS3",
            bundle="aws",
            sched="15 sec",
            props=s3_props(
                creds, **{"Bucket": "#{bucket_landing}", "Prefix": "uploads/", "Minimum Object Age": "5 sec"}
            ),
        ),
        dict(
            name="CheckSize",
            type=STD + "RouteOnAttribute",
            bundle="std",
            props={
                "Routing Strategy": "Route to Property name",
                "too_large": "${s3.length:gt(#{max_upload_bytes})}",
                "empty": "${s3.length:equals(0)}",
            },
        ),
        dict(
            name="FetchUpload",
            type=AWS + "FetchS3Object",
            bundle="aws",
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "#{bucket_landing}", "Object Key": "${filename}"}),
        ),
        dict(name="DetectType", type=STD + "IdentifyMimeType", bundle="std", props={}),
        dict(
            name="IsVideo",
            type=STD + "RouteOnAttribute",
            bundle="std",
            props={"Routing Strategy": "Route to Property name", "video": "${mime.type:startsWith('video/')}"},
        ),
        dict(
            name="Checksum",
            type=STD + "CryptographicHashContent",
            bundle="std",
            props={"Hash Algorithm": "SHA-256", "Fail When Content Empty": "true"},
        ),
        dict(
            name="ComputeKeys",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={
                "sha256": "${content_SHA-256}",
                "landing_key": "${filename}",
                "original_name": "${filename:substringAfterLast('/')}",
                "bronze_bucket": "#{bucket_bronze}",
                "bronze_key": "videos/${content_SHA-256}/original.${filename:substringAfterLast('.'):toLower()}",
                "size_bytes": "${s3.length}",
                "mime_type": "${mime.type}",
                "duplicate": "false",
                "ingest_ts": TS,
            },
        ),
        dict(
            name="CheckDuplicate",
            type=AWS + "GetS3ObjectMetadata",
            bundle="aws",
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "${bronze_bucket}", "Object Key": "${bronze_key}"}),
        ),
        dict(
            name="MarkDuplicate",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={"duplicate": "true"},
        ),
        dict(
            name="PutOriginal",
            type=AWS + "PutS3Object",
            bundle="aws",
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "${bronze_bucket}", "Object Key": "${bronze_key}"}),
        ),
        dict(
            name="ManifestKey",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={"manifest_key": "ingest/${sha256}/${ingest_ts}-${uuid}.json"},
        ),
        dict(
            name="ToJSON",
            type=STD + "AttributesToJSON",
            bundle="std",
            props={
                "Destination": "flowfile-content",
                "Include Core Attributes": "false",
                "Attributes List": "sha256,bronze_bucket,bronze_key,landing_key,original_name,size_bytes,"
                "mime_type,duplicate,manifest_key,ingest_ts",
            },
        ),
        dict(
            name="PutManifest",
            type=AWS + "PutS3Object",
            bundle="aws",
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "#{bucket_manifests}", "Object Key": "${manifest_key}"}),
        ),
        dict(
            name="NotifyAPI",
            type=STD + "InvokeHTTP",
            bundle="std",
            auto=["Response"],
            extra=retry_penalty,
            sensitive=["Authorization"],
            props={
                "HTTP Method": "POST",
                "HTTP URL": "#{api_ingest_url}",
                "Request Content-Type": "application/json",
                "Response Body Attribute Name": "api_response",
                "Authorization": "#{ingest_auth_header}",
            },
        ),
        dict(
            name="RetryAPI",
            type=STD + "RetryFlowFile",
            bundle="std",
            props={"Maximum Retries": "120", "Penalize Retries": "true"},
        ),
        dict(
            name="GiveUpAPI",
            type=STD + "LogAttribute",
            bundle="std",
            auto=["success"],
            props={"Log Level": "error", "Log Payload": "false"},
        ),
        dict(
            name="DeleteLanding",
            type=AWS + "DeleteS3Object",
            bundle="aws",
            auto=["success"],
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "#{bucket_landing}", "Object Key": "${landing_key}"}),
        ),
        # ── rejection path
        dict(
            name="RejectInfo",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={
                "landing_key": "${landing_key:replaceNull(${filename})}",
                "reject_key": "rejected/" + TS + "-${filename:substringAfterLast('/')}",
                "reject_reason": "${reject_reason:replaceNull('validation failed')}",
                "manifest_key": "rejected/" + TS + "-${uuid}.json",
            },
        ),
        dict(
            name="CopyToRejected",
            type=AWS + "CopyS3Object",
            bundle="aws",
            extra=retry_penalty,
            props={
                **s3_props(creds),
                "Source Bucket": "#{bucket_landing}",
                "Source Key": "${landing_key}",
                "Destination Bucket": "#{bucket_landing}",
                "Destination Key": "${reject_key}",
            },
        ),
        dict(
            name="RejectJSON",
            type=STD + "AttributesToJSON",
            bundle="std",
            props={
                "Destination": "flowfile-content",
                "Include Core Attributes": "false",
                "Attributes List": "landing_key,reject_key,reject_reason,mime.type,s3.length,sha256,api_response",
            },
        ),
        dict(
            name="PutRejectManifest",
            type=AWS + "PutS3Object",
            bundle="aws",
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "#{bucket_manifests}", "Object Key": "${manifest_key}"}),
        ),
        dict(
            name="DeleteRejected",
            type=AWS + "DeleteS3Object",
            bundle="aws",
            auto=["success"],
            extra=retry_penalty,
            props=s3_props(creds, **{"Bucket": "#{bucket_landing}", "Object Key": "${landing_key}"}),
        ),
        dict(
            name="ReasonTooLarge",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={"reject_reason": "size ${s3.length} bytes is empty or exceeds #{max_upload_bytes}"},
        ),
        dict(
            name="ReasonType",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={"reject_reason": "unsupported content type ${mime.type} (MVP accepts video only)"},
        ),
        dict(
            name="ReasonAPI",
            type="org.apache.nifi.processors.attributes.UpdateAttribute",
            bundle="upd",
            props={"reject_reason": "API rejected: HTTP ${invokehttp.status.code}"},
        ),
    ]


CONNECTIONS = [  # (source, relationships, destination)
    ("ListLanding", ["success"], "CheckSize"),
    ("CheckSize", ["unmatched"], "FetchUpload"),
    ("CheckSize", ["too_large", "empty"], "ReasonTooLarge"),
    ("FetchUpload", ["success"], "DetectType"),
    ("FetchUpload", ["failure"], "FetchUpload"),
    ("DetectType", ["success"], "IsVideo"),
    ("IsVideo", ["video"], "Checksum"),
    ("IsVideo", ["unmatched"], "ReasonType"),
    ("Checksum", ["success"], "ComputeKeys"),
    ("Checksum", ["failure"], "ReasonType"),
    ("ComputeKeys", ["success"], "CheckDuplicate"),
    ("CheckDuplicate", ["found"], "MarkDuplicate"),
    ("CheckDuplicate", ["not found"], "PutOriginal"),
    ("CheckDuplicate", ["failure"], "CheckDuplicate"),
    ("MarkDuplicate", ["success"], "ManifestKey"),
    ("PutOriginal", ["success"], "ManifestKey"),
    ("PutOriginal", ["failure"], "PutOriginal"),
    ("ManifestKey", ["success"], "ToJSON"),
    ("ToJSON", ["success"], "PutManifest"),
    ("ToJSON", ["failure"], "RejectInfo"),
    ("PutManifest", ["success"], "NotifyAPI"),
    ("PutManifest", ["failure"], "PutManifest"),
    ("NotifyAPI", ["Original"], "DeleteLanding"),
    ("NotifyAPI", ["Retry", "Failure"], "RetryAPI"),
    ("NotifyAPI", ["No Retry"], "ReasonAPI"),
    ("RetryAPI", ["retry"], "NotifyAPI"),
    ("RetryAPI", ["retries_exceeded", "failure"], "GiveUpAPI"),
    ("DeleteLanding", ["failure"], "DeleteLanding"),
    ("ReasonTooLarge", ["success"], "RejectInfo"),
    ("ReasonType", ["success"], "RejectInfo"),
    ("ReasonAPI", ["success"], "RejectInfo"),
    ("RejectInfo", ["success"], "CopyToRejected"),
    ("CopyToRejected", ["success"], "RejectJSON"),
    ("CopyToRejected", ["failure"], "CopyToRejected"),
    ("RejectJSON", ["success"], "PutRejectManifest"),
    ("RejectJSON", ["failure"], "DeleteRejected"),
    ("PutRejectManifest", ["success"], "DeleteRejected"),
    ("PutRejectManifest", ["failure"], "PutRejectManifest"),
    ("DeleteRejected", ["failure"], "DeleteRejected"),
]


def create_processor(nifi: Nifi, pg_id: str, spec: dict, pos: tuple[int, int]) -> str:
    p = nifi.req(
        "POST",
        f"/process-groups/{pg_id}/processors",
        json={
            "revision": {"version": 0},
            "component": {
                "type": spec["type"],
                "bundle": BUNDLES[spec["bundle"]],
                "name": spec["name"],
                "position": {"x": pos[0], "y": pos[1]},
            },
        },
    )
    descriptors = p["component"]["config"]["descriptors"]
    by_display = {d["displayName"]: n for n, d in descriptors.items()}
    props = {by_display.get(k, k): v for k, v in spec["props"].items()}  # unknown keys = dynamic properties
    config = {"properties": props, "autoTerminatedRelationships": spec.get("auto", []), **spec.get("extra", {})}
    if spec.get("sched"):
        config["schedulingPeriod"] = spec["sched"]
    if spec.get("sensitive"):
        config["sensitiveDynamicPropertyNames"] = spec["sensitive"]
    p = nifi.req(
        "PUT",
        f"/processors/{p['id']}",
        json={"revision": p["revision"], "component": {"id": p["id"], "config": config}},
    )
    return p["id"]


def connect(nifi: Nifi, pg_id: str, src: str, rels: list[str], dst: str) -> None:
    nifi.req(
        "POST",
        f"/process-groups/{pg_id}/connections",
        json={
            "revision": {"version": 0},
            "component": {
                "source": {"id": src, "groupId": pg_id, "type": "PROCESSOR"},
                "destination": {"id": dst, "groupId": pg_id, "type": "PROCESSOR"},
                "selectedRelationships": rels,
                "backPressureObjectThreshold": 1000,
                "backPressureDataSizeThreshold": "4 GB",
            },
        },
    )


def delete_pg(nifi: Nifi, pg: dict) -> None:
    pid = pg["id"]
    nifi.run_status(pid, "STOPPED")
    nifi.services_state(pid, "DISABLED")
    drop = nifi.req("POST", f"/process-groups/{pid}/empty-all-connections-requests")
    did = drop["dropRequest"]["id"]
    for _ in range(60):
        if nifi.req("GET", f"/process-groups/{pid}/empty-all-connections-requests/{did}")["dropRequest"]["finished"]:
            break
        time.sleep(1)
    nifi.req("DELETE", f"/process-groups/{pid}/empty-all-connections-requests/{did}")
    time.sleep(2)
    latest = nifi.req("GET", f"/process-groups/{pid}")
    nifi.req("DELETE", f"/process-groups/{pid}", params={"version": latest["revision"]["version"]})


def relist(nifi: Nifi, pg: dict) -> None:
    pid = pg["id"]
    procs = nifi.req("GET", f"/process-groups/{pid}/processors")["processors"]
    lister = next(p for p in procs if p["component"]["name"] == "ListLanding")
    nifi.req("PUT", f"/processors/{lister['id']}/run-status", json={"revision": lister["revision"], "state": "STOPPED"})
    time.sleep(2)
    nifi.req("POST", f"/processors/{lister['id']}/state/clear-requests")
    lister = nifi.req("GET", f"/processors/{lister['id']}")
    nifi.req("PUT", f"/processors/{lister['id']}/run-status", json={"revision": lister["revision"], "state": "RUNNING"})
    print("ListS3 state cleared; objects remaining in landing/uploads/ will be re-ingested.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://localhost:8443/nifi-api")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--relist", action="store_true")
    args = ap.parse_args()
    env = load_env()
    nifi = Nifi(args.url, env["NIFI_ADMIN_USER"], env["NIFI_ADMIN_PASSWORD"])
    root = nifi.root_id()
    existing = nifi.find_pg(root, PG_NAME)

    if args.relist:
        if not existing:
            print("flow not deployed", file=sys.stderr)
            return 1
        relist(nifi, existing)
        return 0
    if existing and not args.replace:
        print(f"'{PG_NAME}' already deployed (id {existing['id']}). Use --replace to recreate.")
        return 0
    if existing:
        delete_pg(nifi, existing)
        print("deleted previous flow")
    if args.replace and (ctx := nifi.find_ctx(CTX_NAME)):
        # Parameter sensitivity cannot change in place; recreate the context from .env.
        nifi.req("DELETE", f"/parameter-contexts/{ctx['id']}", params={"version": ctx["revision"]["version"]})

    ctx = ensure_param_context(nifi, env)
    pg = create_pg(nifi, root, ctx)
    creds = create_credentials(nifi, pg)
    ids: dict[str, str] = {}
    for i, spec in enumerate(processor_specs(creds)):
        col, row = (0, i) if i < 17 else (1, i - 17)
        ids[spec["name"]] = create_processor(nifi, pg, spec, (col * 700, row * 220))
    for src, rels, dst in CONNECTIONS:
        connect(nifi, pg, ids[src], rels, ids[dst])

    nifi.services_state(pg, "ENABLED")
    time.sleep(3)
    invalid = [
        (p["component"]["name"], p["component"].get("validationErrors"))
        for p in nifi.req("GET", f"/process-groups/{pg}/processors")["processors"]
        if p["component"].get("validationStatus") == "INVALID"
    ]
    if invalid:
        for name, errs in invalid:
            print(f"INVALID {name}: {errs}", file=sys.stderr)
        return 1
    nifi.run_status(pg, "RUNNING")
    print(f"deployed '{PG_NAME}' ({len(ids)} processors, {len(CONNECTIONS)} connections) and started it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
