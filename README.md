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
| Summary per tool (total / success / warning / failed / running) | EM `BackupJobSession` + `BackupTaskSession`. One line per workload, **final result after retries** |
| Failure table: location, server, job, consecutive failures | Location = trigram at the start of the job name (`JAB-B-LR` → `JAB`). Consecutive failures = consecutive days with a failure, read from task history (EM keeps ~30 days) |
| "All Servers" rows | Automatic when every workload of a job failed |
| Mitigation yes/no + comments | `comments.csv`, filled by L1/L2. Failures without a comment are shown as **Awaiting L1/L2 comment** |
| Backup Copy / DR Replication | Session counts per tool and status (Healthy, or the failed jobs) |
| Jobs to check | Jobs with schedule disabled, or enabled but no session since the report day |
| Repositories low on space | EM `Repository` free space under the threshold |
| Footer notes | `comments.csv` rows with `scope=note` |

## Setup

1. `cp config.example.yaml config.yaml` and set: EM URL, report timezone, VBR server names per tool, SMTP.
2. Create an EM service account and store it in Jenkins as the `veeam-em-report` credential.
3. `cp comments.example.csv comments.csv` (or point `comments_file` to a share L1/L2 can edit).
4. Preview without EM: `python -m vdr --config config.yaml --demo`.

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
| tool | `DCU`, `Premium` or label; wildcard `*` |
| job / server | job name and workload name; wildcards allowed (`JAB-B-*`) |
| mitigation | `Yes` / `No` |
| comment | free text |
| expires | optional `YYYY-MM-DD`; the row disappears after that date |

## Adding NetBackup later

Write `vdr/collector_netbackup.py` returning the same `Collected` model (NetBackup REST `/netbackup/admin/jobs`),
add a `tools` entry, and merge both collections in `__main__.py`.
