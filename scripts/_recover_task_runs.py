"""One-off recovery: restore the scheduled_task_runs rows destroyed when the
curation pass deleted 18 tasks (scheduled_task_runs.task_id is ON DELETE CASCADE).

Non-destructive to the live cluster: Aurora PITR restores into a NEW cluster,
which is read, harvested, and then torn down.

    python scripts/_recover_task_runs.py retention   # raise backup retention to 14d
    python scripts/_recover_task_runs.py config      # show source cluster config
    python scripts/_recover_task_runs.py restore     # start the PITR restore
    python scripts/_recover_task_runs.py status      # poll the clone
    python scripts/_recover_task_runs.py harvest     # read lost runs out of the clone
    python scripts/_recover_task_runs.py reinsert    # write them back to the live DB
    python scripts/_recover_task_runs.py teardown    # delete the clone
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import boto3  # noqa: E402

REGION = os.getenv("AWS_REGION", "us-east-1")
SRC_CLUSTER = "gandi-assistant-db"
CLONE_CLUSTER = "gandi-assistant-db-recover"
CLONE_INSTANCE = "gandi-assistant-db-recover-1"

# 08:57:10-08:57:29 UTC is when the deletes ran; 08:50 is safely before.
RESTORE_TO = datetime(2026, 9, 14, 8, 50, 0, tzinfo=timezone.utc)

HARVEST_PATH = os.path.join(ROOT, "recovered_task_runs.json")

rds = boto3.client("rds", region_name=REGION)


def _src():
    return rds.describe_db_clusters(DBClusterIdentifier=SRC_CLUSTER)["DBClusters"][0]


def cmd_retention():
    for cid in (SRC_CLUSTER, "gandi-assistant-auth"):
        try:
            r = rds.modify_db_cluster(
                DBClusterIdentifier=cid, BackupRetentionPeriod=14, ApplyImmediately=True
            )
            print(f"{cid}: retention -> {r['DBCluster']['BackupRetentionPeriod']} days")
        except Exception as exc:  # noqa: BLE001
            print(f"{cid}: ERROR {exc}")


def cmd_config():
    cl = _src()
    for k in ("Engine", "EngineVersion", "EngineMode", "Port", "DatabaseName",
              "MasterUsername", "StorageEncrypted", "EarliestRestorableTime",
              "LatestRestorableTime", "BackupRetentionPeriod"):
        print(f"  {k} = {cl.get(k)}")
    print("  DBSubnetGroup =", cl.get("DBSubnetGroup"))
    print("  SecurityGroups =", [g["VpcSecurityGroupId"] for g in cl.get("VpcSecurityGroups", [])])
    print("  ServerlessV2 =", cl.get("ServerlessV2ScalingConfiguration"))
    members = [(m["DBInstanceIdentifier"], m["IsClusterWriter"]) for m in cl.get("DBClusterMembers", [])]
    print("  members =", members)
    if members:
        i = rds.describe_db_instances(DBInstanceIdentifier=members[0][0])["DBInstances"][0]
        print("  instance class =", i["DBInstanceClass"])


def cmd_restore():
    cl = _src()
    earliest, latest = cl.get("EarliestRestorableTime"), cl.get("LatestRestorableTime")
    print(f"window: {earliest} .. {latest}")
    if earliest and RESTORE_TO < earliest:
        sys.exit(f"ABORT: restore point {RESTORE_TO} is older than the window. Too late.")

    # This account's describe_* responses come back without DBSubnetGroup or
    # VpcSecurityGroups, so pass them only if they are actually present. Omitted,
    # RestoreDBClusterToPointInTime inherits the source cluster's networking,
    # which is what we want anyway.
    kwargs = dict(
        SourceDBClusterIdentifier=SRC_CLUSTER,
        DBClusterIdentifier=CLONE_CLUSTER,
        RestoreToTime=RESTORE_TO,
        Tags=[{"Key": "purpose", "Value": "task-run-history-recovery"}],
    )
    if cl.get("DBSubnetGroup"):
        kwargs["DBSubnetGroupName"] = cl["DBSubnetGroup"]
    sgs = [g["VpcSecurityGroupId"] for g in cl.get("VpcSecurityGroups", [])]
    if sgs:
        kwargs["VpcSecurityGroupIds"] = sgs
    if cl.get("ServerlessV2ScalingConfiguration"):
        kwargs["ServerlessV2ScalingConfiguration"] = cl["ServerlessV2ScalingConfiguration"]
    rds.restore_db_cluster_to_point_in_time(**kwargs)
    print(f"restoring {CLONE_CLUSTER} to {RESTORE_TO.isoformat()} ...")

    members = cl.get("DBClusterMembers", [])
    src_inst = rds.describe_db_instances(
        DBInstanceIdentifier=members[0]["DBInstanceIdentifier"]
    )["DBInstances"][0]
    rds.create_db_instance(
        DBInstanceIdentifier=CLONE_INSTANCE,
        DBClusterIdentifier=CLONE_CLUSTER,
        DBInstanceClass=src_inst["DBInstanceClass"],
        Engine=cl["Engine"],
    )
    print(f"creating instance {CLONE_INSTANCE} ({src_inst['DBInstanceClass']}) ...")


def cmd_status():
    try:
        cl = rds.describe_db_clusters(DBClusterIdentifier=CLONE_CLUSTER)["DBClusters"][0]
        print("cluster:", cl["Status"], "| endpoint:", cl.get("Endpoint"))
    except Exception as exc:  # noqa: BLE001
        print("cluster: not found —", str(exc)[:90])
        return
    try:
        i = rds.describe_db_instances(DBInstanceIdentifier=CLONE_INSTANCE)["DBInstances"][0]
        print("instance:", i["DBInstanceStatus"])
    except Exception as exc:  # noqa: BLE001
        print("instance: not found —", str(exc)[:90])


def _clone_conn():
    """Connect to the restored clone, mirroring hermes.db.connection's auth:
    a static DB_PASSWORD/PGPASSWORD if set, otherwise a fresh RDS IAM token
    minted for the CLONE's hostname (an IAM token is host-specific)."""
    import psycopg

    # Use the INSTANCE endpoint, not the cluster one: DBRDS_HOST points at the
    # source's instance endpoint, and the RDS IAM auth token is minted per host,
    # so it has to match whatever we actually dial.
    inst = rds.describe_db_instances(DBInstanceIdentifier=CLONE_INSTANCE)["DBInstances"][0]
    host = inst["Endpoint"]["Address"]
    port = int(os.getenv("DBRDS_PORT", "5432") or 5432)
    user = os.getenv("DBRDS_USER", "").strip() or "postgres"

    pw = os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD")
    if not pw:
        pw = rds.generate_db_auth_token(
            DBHostname=host, Port=port, DBUsername=user, Region=REGION
        )
    return psycopg.connect(
        host=host, port=port, user=user, password=pw,
        dbname=os.getenv("DB_NAME", "postgres").strip() or "postgres",
        sslmode=os.getenv("PGSSLMODE", "require").strip() or "require",
        connect_timeout=20,
    )


def cmd_harvest():
    """Pull every run row from the clone whose task no longer exists live."""
    from hermes.db.connection import cursor

    with cursor(commit=False) as cur:
        cur.execute("SELECT task_id FROM scheduled_tasks")
        live_tasks = {str(r[0]) for r in cur.fetchall()}
        cur.execute("SELECT run_id FROM scheduled_task_runs")
        live_runs = {str(r[0]) for r in cur.fetchall()}
    print(f"live now: {len(live_tasks)} tasks, {len(live_runs)} runs")

    with _clone_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM scheduled_tasks")
        tcols = [d[0] for d in cur.description]
        old_tasks = {str(dict(zip(tcols, r))["task_id"]): dict(zip(tcols, r)) for r in cur.fetchall()}
        cur.execute("SELECT * FROM scheduled_task_runs ORDER BY started_at")
        rcols = [d[0] for d in cur.description]
        old_runs = [dict(zip(rcols, r)) for r in cur.fetchall()]

    print(f"clone: {len(old_tasks)} tasks, {len(old_runs)} runs")
    lost = [r for r in old_runs if str(r["run_id"]) not in live_runs]
    print(f"runs missing from live: {len(lost)}")

    out = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "restore_point": RESTORE_TO.isoformat(),
        "lost_runs": lost,
        "their_tasks": {tid: t for tid, t in old_tasks.items()
                        if tid in {str(r["task_id"]) for r in lost}},
    }
    with open(HARVEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=str, ensure_ascii=False)

    by_task: dict[str, int] = {}
    for r in lost:
        by_task[str(r["task_id"])] = by_task.get(str(r["task_id"]), 0) + 1
    print("\nlost runs by task:")
    for tid, n in sorted(by_task.items(), key=lambda kv: -kv[1]):
        name = old_tasks.get(tid, {}).get("name", "?")
        chars = sum(len(r.get("reply") or "") for r in lost if str(r["task_id"]) == tid)
        print(f"  {n:>2} run(s), {chars:>6} chars of report — {name}")
    print(f"\nwritten to {HARVEST_PATH}")


def cmd_reinsert():
    """Re-create the deleted tasks (disabled) and re-attach their runs.

    The runs cannot exist without their parent row — that is the cascade that
    destroyed them. Each parent is restored DISABLED with a [RECOVERED] prefix so
    it is archive, not schedule: nothing starts firing again."""
    from hermes.db.connection import cursor

    with open(HARVEST_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    lost, their_tasks = data["lost_runs"], data["their_tasks"]
    if not lost:
        print("nothing to reinsert")
        return

    tasks_done = runs_done = 0
    with cursor() as cur:
        for tid, t in their_tasks.items():
            name = t.get("name") or "Untitled"
            cur.execute(
                """INSERT INTO scheduled_tasks
                     (task_id, user_id, entity_id, name, prompt, allowed_apps, autonomy,
                      schedule, timezone, enabled, next_run_at, notify_to, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,false,NULL,NULL,%s)
                   ON CONFLICT (task_id) DO NOTHING;""",
                (tid, t["user_id"], t.get("entity_id"), f"[RECOVERED] {name}",
                 t.get("prompt") or "", json.dumps(t.get("allowed_apps") or []),
                 t.get("autonomy") or "readonly", json.dumps(t.get("schedule") or {}),
                 t.get("timezone") or "America/Chicago", t.get("created_at")),
            )
            tasks_done += cur.rowcount
        for r in lost:
            cur.execute(
                """INSERT INTO scheduled_task_runs
                     (run_id, task_id, trigger, status, reply, tool_calls,
                      error, started_at, finished_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO NOTHING;""",
                (r["run_id"], r["task_id"], r.get("trigger") or "schedule",
                 r.get("status") or "success", r.get("reply"),
                 json.dumps(r.get("tool_calls")) if r.get("tool_calls") is not None else None,
                 r.get("error"), r.get("started_at"), r.get("finished_at")),
            )
            runs_done += cur.rowcount
    print(f"reinserted {tasks_done} archive task row(s) and {runs_done} run(s)")

    with cursor(commit=False) as cur:
        cur.execute("SELECT count(*) FROM scheduled_task_runs")
        print("live run rows now:", cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM scheduled_tasks WHERE enabled")
        print("enabled tasks (should still be 25):", cur.fetchone()[0])


def cmd_teardown():
    try:
        rds.delete_db_instance(DBInstanceIdentifier=CLONE_INSTANCE, SkipFinalSnapshot=True)
        print("deleting instance", CLONE_INSTANCE)
    except Exception as exc:  # noqa: BLE001
        print("instance:", str(exc)[:110])
    try:
        rds.delete_db_cluster(DBClusterIdentifier=CLONE_CLUSTER, SkipFinalSnapshot=True)
        print("deleting cluster", CLONE_CLUSTER)
    except Exception as exc:  # noqa: BLE001
        print("cluster:", str(exc)[:110])


CMDS = {
    "retention": cmd_retention, "config": cmd_config, "restore": cmd_restore,
    "status": cmd_status, "harvest": cmd_harvest, "reinsert": cmd_reinsert,
    "teardown": cmd_teardown,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit(__doc__)
    CMDS[sys.argv[1]]()
