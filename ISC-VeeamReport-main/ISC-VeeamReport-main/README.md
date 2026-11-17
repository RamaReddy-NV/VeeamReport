# Veeam daily backup report

Python job (run from Jenkins) that builds the daily backup table from **Veeam Backup Enterprise Manager**
(one REST endpoint for all 3 VBR servers) and e-mails it as HTML.

```
Jenkins (07:00) ──► python -m vdr ──► EM REST API :9398 ──► vbr-dcu01, vbr-prm01, vbr-prm02
                         │
                         ├── comments.csv  (L1/L2 comments, mitigation, footer notes)
                         └──► out/report.html (mail body) + out/report.json (archive / trends)
```

## What is automatic vs manual

| Report part | Source |
|---|---|
| Summary per tool (workload backups / success / warning / failed / running) | EM `BackupJobSession` + `BackupTaskSession`. One line per workload/job pair, **final result after retries** |
| Failure table: location, server, job, consecutive failures | Location = trigram at the start of the job name (`JAB-B-LR` → `JAB`). Consecutive failures = consecutive days with a failure, read from task history (EM keeps ~30 days) |
| "All Servers" rows | Automatic when every workload of a job failed |
| Mitigation yes/no + comments | `comments.csv`, filled by L1/L2. Failures without a comment are shown as **Awaiting L1/L2 comment** |
| Backup Copy / DR Replication | Session counts per tool and status (Healthy, or the failed jobs) |
| Jobs to check | Jobs with a configured schedule disabled, or enabled but no session since the report day |
| Repositories low on space | EM `Repository` free space under the threshold |
| Footer notes | `comments.csv` rows with `scope=note` |

## Setup

1. Configure `config.yaml` with the EM URL, report timezone, VBR server names, and approved SMTP relay.
2. Create an EM service account and store it in Jenkins as the `veeam-em-report` credential. Credentials are read from `VEEAM_EM_USER` and `VEEAM_EM_PASSWORD`; do not put credentials in YAML.
3. Maintain `comments.csv`, or point `comments_file` to a share L1/L2 can edit.
4. Preview without EM: `python -m vdr --config config.yaml --demo`.
5. Install the pinned runtime dependencies with `pip install --require-hashes -r requirements.lock`.

The project supports Python 3.10 and newer. The Jenkins production and test pipelines
currently use Python 3.11. The daily pipeline installs only runtime dependencies;
tests run through the separate `Jenkinsfile-test` pipeline (configure that Jenkins
job's Script Path as `Jenkinsfile-test`).

The EM API requires HTTPS and certificate verification is enabled by default. Keep
`verify_tls: true`; install the issuing CA in the runtime trust store if the EM
certificate uses an internal CA. SMTP delivery requires STARTTLS; confirm the
approved relay supports it. SMTP authentication reads
credentials from `SMTP_USER` and `SMTP_PASSWORD`. Raw API dumps and generated JSON
reports may contain workload names and error details; store them with appropriate
access controls and avoid retaining raw dumps longer than needed.

## Tests

Install test dependencies with
`pip install --require-hashes -r requirements-dev.lock`, then run `python -m pytest`.
The test pipeline enforces the 80% total coverage minimum, checks types with mypy,
and runs the Ruff zero-tolerance gates. To refresh dependency locks, install
`pip-tools` in a Python 3.10 development environment and run:

```text
python3.10 -m piptools compile --generate-hashes --output-file=requirements.lock requirements.txt
python3.10 -m piptools compile --generate-hashes --output-file=requirements-dev.lock requirements-dev.txt
```

The test pipeline can also check live Enterprise Manager assumptions. Enable
`RUN_EM_INTEGRATION` for the build and provide the `veeam-em-report` Jenkins
credential. The report date defaults to yesterday; `EM_INTEGRATION_RETRY_JOB` and
`EM_INTEGRATION_DISABLED_JOB` can select known example jobs, and
`EM_INTEGRATION_TERMINAL_STATES` can list comma-separated finished states already
confirmed in the VBR console. The integration test prints the observed ordering,
state values, job types, retry, and schedule checks. Raw first-page API dumps stay
under `.em-integration/raw` on the agent and are not archived. Review the printed
state mapping against the VBR console and add any missing finished states to
`DONE_STATES` in `vdr/build.py`.

Each configured tool has a `min_workloads` threshold (default `1`). If collection
falls below it, the report marks that tool `NO DATA`, still writes and sends the
report, and exits with status `3` so Jenkins alerts the on-call team. Consecutive
failure history lookups are capped at 50 workloads per report by
`failures.max_history_lookups`.

## First run against your Enterprise Manager (validation)

```bash
export VEEAM_EM_USER=... VEEAM_EM_PASSWORD=...
python -m vdr --config config.yaml --date 2026-10-26 --dump-raw out/raw -v
```

Then check, and compare with the same day in the VBR console:

- [ ] `out/raw/*.json`: field names match what the collector reads (`JobName`, `JobType`, `IsRetry`, `VmDisplayName`,
      `Result`, `Reason`, `Links`...). EM JSON is generated from its XML schema; if a field is named differently, it is a one-line fix in `vdr/collector_em.py`.
- [ ] "Data quality" line at the bottom of the report: add any unmapped VBR server or job type to `config.yaml`.
- [ ] Totals per tool match the console for a couple of jobs, including one with a retry.
- [ ] Agent jobs: check they appear in the totals (EM returns them with backup sessions; confirm their task sessions too).
- [ ] Consecutive failure count on a workload you know has been failing for several days.

## Known limits of the EM API

- Workloads EM does not model as task sessions (file share / NAS, enterprise application plug-ins such as RMAN/SAP HANA)
  will not show in the per-workload counts. If you have them, a small PowerShell exporter on a Windows node
  (`Get-VBRTaskSession`) can feed the same model.
- Session history in EM is limited (~30 days), so consecutive failures cap there.

## comments.csv

| column | meaning |
|---|---|
| scope | `failure` (failure table), `summary` (summary Comments column), `note` (footer line) |
| tool | `DCU`, `Premium`, `PremiumDR` or label; wildcards such as `Premium*` |
| job / server | job name and workload name; wildcards allowed (`JAB-B-*`) |
| mitigation | `Yes` / `No` |
| comment | free text |
| expires | optional `YYYY-MM-DD`; the row disappears after that date |

The comments loader accepts UTF-8 (with or without BOM) and Windows-1252 files,
and detects comma or semicolon delimiters. Invalid headers, skipped rows, and
comments that match no report row are surfaced in the report's Data quality line.

## Adding NetBackup later

Write `vdr/collector_netbackup.py` returning the same `Collected` model (NetBackup REST `/netbackup/admin/jobs`),
add a `tools` entry, and merge both collections in `__main__.py`.
