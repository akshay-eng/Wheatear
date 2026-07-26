# Catalog probes

Throwaway diagnostic scripts, not shipped code. They exist because every claim
in `connectors/orchestrate/catalog_client.py` about how the watsonx Orchestrate
catalog behaves was established by running one of these against the live
service, not read from documentation.

Keep them: if IBM changes the catalog, re-running these is how you find out
what changed and why.

| script | what it established |
|---|---|
| `probe_catalog.py` | The catalog rejects an IAM bearer token (500 `WXO-PROXY-11076E`). |
| `probe_tenant.py` | Tenant id derives from the IAM JWT's `account.bss` + instance id, and supplying it as header/cookie/CRN still doesn't authorise. |
| `probe_instance_catalog.py` | `/v1/orchestrate/catalog/artifacts` exists on the instance API and *is* IAM-authenticated, but is tenant-scoped and returns `total: 0`. POST to it is the install path (403 without write permission). |
| `probe_scope.py` | Valid categories are exactly `tool`, `agent`, `mcp_server`. No scoping parameter makes the instance route return the global catalog. |
| `probe_hardcodes.py` | `select_fields` is optional but its default drops `tags`/`type`; a bogus `type` value 400s the whole query; the page-size ceiling is 1000 and the service states it; all categories can be fetched in one query. |
| `probe_detail.py`, `probe_spec.py` | `GET /artifacts/{id}` returns `spec_file` with `input_schema`, `applications` (required connections) and `mcp.tools`. |
| `inspect_ranking.py` | Diagnosed the two matcher defects: plurals weren't folded (`records` ≠ `record`) and verbs were stopwords, so a read scored identically to a destructive write. |
| `recall_check.py` | Shortlist recall over the live 1173-entry catalog. |
| `verify_full_catalog.py`, `verify_final.py` | Full-sweep verification: 1152 tools + 21 MCP servers + 337 agents. |
| `e2e_catalog.py` | Drives the real `match-tools` CLI with the network layer replayed from a HAR. |
| `diag_enrich.py` | Unfinished — was diagnosing why 988/1173 detail fetches failed in the bulk enrich run. |

## Running them

They need `IBMUrl`/`IBMKey`, and most need a console session cookie. `run_with_env.py`
loads both from fish universal variables and pulls a cookie out of a HAR capture:

```bash
python scripts/probes/run_with_env.py --cookie python scripts/probes/probe_scope.py
```

The HAR path is hardcoded inside `run_with_env.py`. **That file contains a live
session cookie** — treat any HAR from the console as a credential.