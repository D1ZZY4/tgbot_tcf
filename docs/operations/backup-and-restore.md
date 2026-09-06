# Backup and Restore Runbook

Federation bans, staff roles, and connected groups are irreplaceable operational
data. Loss of any of these collections requires manual reconstruction from
moderator memory and audit logs. This document describes how to protect and
recover that data.

## Critical collections

| Collection | Contents | Loss impact |
|---|---|---|
| `bans` | All active and historical federation bans | Critical: bans cannot be reconstructed from Telegram |
| `tc_owners` / `tc_admins` / `tc_roles` | Staff role assignments | Critical: roles must be re-granted manually |
| `federated_groups` | Connected group registry | High: reconnecting requires each group admin to re-run `/tcconnect` |
| `warns` / `warn_counts` | Warning records and counters | Medium: warn history lost; users get a clean slate |
| `apscheduler_jobs` | Persistent scheduler data | Medium: scheduler state may need verification after restore |

---

## MongoDB Atlas or another managed provider

Atlas backup products or scheduled `mongodump` archives can protect the
database. The exact backup features, retention, and eligible cluster tiers
depend on the Atlas plan currently in use.

### Enable backup

1. In the Atlas console, open your cluster → **Backup**.
2. Enable the backup option available for the cluster and plan.
3. Choose retention that matches the recovery requirements of your deployment.
4. Verify that backup is **Active** in the cluster overview.

### Point-in-time restore

1. In the Atlas console, open the cluster → **Backup → Snapshots** (or the
   continuous-restore timeline).
2. Select the desired restore point.
3. Choose **Restore to this cluster** (overwrites existing) or
   **Restore to new cluster** (safer, then swap connection string).
4. Monitor progress in the **Restore Jobs** tab.
5. After restore, verify counts:
   ```
    db.bans.countDocuments()
    db.federated_groups.countDocuments()
   db.tc_roles.countDocuments()
    db.warns.countDocuments()
    db.warn_counts.countDocuments()
   ```

---

## Scheduled `mongodump` archives

For deployments without a suitable managed backup, schedule a nightly
`mongodump` or use the backup tooling provided by your MongoDB host.

### Prerequisites

```bash
# Debian / Ubuntu
sudo apt-get install -y mongodb-tools

# or via the MongoDB tools tarball from https://www.mongodb.com/try/download/tools
```

### Backup script

Create `/opt/tcbot/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

MONGODB_URI="${MONGODB_URI:?MONGODB_URI is not set}"
DB_NAME="${DB_NAME:-tcbot}"
BACKUP_DIR="/opt/tcbot/backups"
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
DEST="${BACKUP_DIR}/${TIMESTAMP}"

mkdir -p "${DEST}"
mongodump --uri="${MONGODB_URI}" --db="${DB_NAME}" --out="${DEST}"
tar -czf "${DEST}.tar.gz" -C "${BACKUP_DIR}" "${TIMESTAMP}"
rm -rf "${DEST}"

# Retain only the last 14 backups
find "${BACKUP_DIR}" -maxdepth 1 -name "*.tar.gz" -printf '%T@ %p\n' \
  | sort -n | head -n -14 | awk '{print $2}' | xargs -r rm -f

echo "[$(date -Iseconds)] Backup complete: ${DEST}.tar.gz"
```

Make it executable:

```bash
chmod +x /opt/tcbot/backup.sh
```

### Schedule with cron

```bash
crontab -e
```

Add a line to run nightly at 02:00 UTC:

```cron
0 2 * * * /opt/tcbot/backup.sh >> /var/log/tcbot-backup.log 2>&1
```

### Offsite copy (optional but strongly recommended)

Pipe the archive to S3, Backblaze B2, or any rclone remote:

```bash
# After the tar line in backup.sh, add:
rclone copy "${DEST}.tar.gz" remote:tcbot-backups/
```

---

## Restore from `mongodump` archive

```bash
# 1. Extract the archive into a temporary directory
DB_NAME="${DB_NAME:-tcbot}"
RESTORE_DIR="$(mktemp -d)"
trap 'rm -rf "${RESTORE_DIR}"' EXIT
tar -xzf /opt/tcbot/backups/20260101T020000.tar.gz -C "${RESTORE_DIR}"

# 2. Restore (additive; does not drop existing data)
mongorestore --uri="${MONGODB_URI}" --db="${DB_NAME}" \
  "${RESTORE_DIR}/20260101T020000/${DB_NAME}/"

# 3. To replace existing data entirely, add --drop
mongorestore --uri="${MONGODB_URI}" --db="${DB_NAME}" --drop \
  "${RESTORE_DIR}/20260101T020000/${DB_NAME}/"
```

---

## Post-restore checklist

After any restore, verify the bot starts cleanly and all subsystems are healthy:

```bash
python -m tcbot &
curl http://localhost:${PORT:-5000}/health
```

Expected response shape (HTTP 200 when all core subsystems are ready):
```json
{
  "status": "ok",
  "mongodb": "ok",
  "redis": "ok",
  "scheduler": "ok",
  "circuit_telegram": "closed",
  "circuit_mongodb": "closed",
  "ts": "2026-01-01T02:00:00+00:00"
}
```

`redis` is `"disabled"` when `REDIS_URL` is not configured. A degraded response
uses HTTP `503`; inspect the individual fields before deciding whether a
restore or a restart is required. Note that `redis` is informational only:
the overall status (and the HTTP code) depends on MongoDB, the scheduler,
and the circuit breakers, so `"redis": "error"` alone still returns 200.
The current ban command does not create
timed-ban schedules, so do not assume that every restore contains unban jobs.

---

## Scheduler dependency note

The project pins APScheduler to `3.11.3` because the scheduler integration is
version-sensitive. Before changing that pin, review the APScheduler release
notes and security advisories, test scheduler startup and restore behavior, and
verify the MongoDB job-store compatibility. Protect the MongoDB account and
backup archives with least-privilege access and network restrictions.
