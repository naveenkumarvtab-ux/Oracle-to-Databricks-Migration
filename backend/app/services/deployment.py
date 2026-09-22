from __future__ import annotations

import json
import re
from datetime import datetime
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import (
    MigrationArtifact,
    MigrationArtifactVersion,
    MigrationClassification,
    MigrationColumn,
    MigrationDependency,
    MigrationIssue,
    MigrationMapping,
    MigrationObject,
    MigrationProject,
    MigrationQualityGate,
    MigrationReview,
    MigrationRun,
    MigrationSource,
    MigrationMedallionNode,
    MigrationStageArtifactVersion,
)
from app.models.canonical import (
    MigrationDeployment,
    MigrationRunStep,
    MigrationValidation,
    MigrationReconciliation,
    MigrationReconciliationDetail,
)
from app.services.databricks_client import execute_sql, databricks_connection
from app.services.engine import compare_schema, topo_order, uid
from app.services.discovery import test_sqlserver_connection
from app.services.source_connector import connector_info, request as connector_request, TableStream
from app.services.type_compatibility import (
    classify_execution_error,
    normalize_row,
    source_select_expression,
    target_parameter_expression,
    transport_contract,
    transport_summary,
)


DEPLOYABLE_TYPES = {"TABLE", "VIEW", "FUNCTION", "PROCEDURE"}
NON_EXECUTABLE_MARKERS = (
    "-- REVIEW_REQUIRED",
    "-- ARCHITECT_REVIEW_REQUIRED",
    "-- RECOMMENDED_TARGET: MANUAL",
    "-- NON_EXECUTABLE:",
)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _payload(row) -> dict[str, Any]:
    try:
        return json.loads(row.payload_json or "{}")
    except Exception:
        return {}


def databricks_workspace_identity() -> str:
    """Return the non-secret identity used to scope target ownership."""
    host = (get_settings().databricks_host or "").strip().lower()
    host = re.sub(r"^https?://", "", host).rstrip("/")
    return host


def _normalized_target_fqn(value: str | None) -> str:
    return (value or "").replace("`", "").strip().lower()


def _target_owner_collision(db: Session, project_id: str, target_fqn: str) -> MigrationDeployment | None:
    """Find an owner of this physical target in the current Databricks workspace.

    Older deployment rows did not record a workspace identity. They cannot safely
    represent ownership after the configured Databricks account changes, so they
    remain audit history but are not used as cross-project ownership locks.
    """
    workspace = databricks_workspace_identity()
    if not workspace:
        return None
    target = _normalized_target_fqn(target_fqn)
    rows = db.scalars(select(MigrationDeployment).where(
        MigrationDeployment.project_id != project_id,
        MigrationDeployment.status == "PASSED",
    )).all()
    for row in rows:
        payload = _payload(row)
        owner_workspace = str(payload.get("databricks_workspace") or "").strip().lower()
        if owner_workspace == workspace and _normalized_target_fqn(payload.get("target_fqn")) == target:
            return row
    return None


def _resolve_stale_ownership_issues(db: Session, project_id: str) -> None:
    """Resolve ownership failures that came from legacy, unscoped deployment rows."""
    changed = False
    issues = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.issue_type == "DEPLOYMENT",
        MigrationIssue.status == "OPEN",
    )).all()
    for issue in issues:
        try:
            details = json.loads(issue.technical_details or "{}")
        except Exception:
            details = {}
        error = str(details.get("error") or "")
        if "Target ownership collision:" not in error:
            continue
        mapping = _mapping(db, project_id, issue.object_id, "DEV") if issue.object_id else None
        if mapping and _target_owner_collision(db, project_id, mapping.target_fqn) is None:
            issue.status = "RESOLVED"
            changed = True
    if changed:
        db.commit()


def _record_step(db: Session, project_id: str, run_id: str, name: str, status: str,
                 object_id: str | None = None, environment: str = "DEV", **details) -> MigrationRunStep:
    row = MigrationRunStep(
        id=uid("STP"), project_id=project_id, object_id=object_id,
        environment=environment, status=status,
        payload_json=_json({"run_id": run_id, "step": name, **details}),
    )
    db.add(row)
    db.commit()
    return row


def _deployment_evidence(db: Session, project_id: str, run_id: str, status: str,
                         environment: str = "DEV", object_id: str | None = None, **details):
    row = MigrationDeployment(
        id=uid("DPL"), project_id=project_id, object_id=object_id,
        environment=environment, status=status,
        payload_json=_json({"run_id": run_id, "databricks_workspace": databricks_workspace_identity(), **details}),
    )
    db.add(row)
    db.commit()
    return row


def _validation_evidence(db: Session, project_id: str, run_id: str, status: str,
                         environment: str = "DEV", object_id: str | None = None, **details):
    row = MigrationValidation(
        id=uid("VAL"), project_id=project_id, object_id=object_id,
        environment=environment, status=status,
        payload_json=_json({"run_id": run_id, **details}),
    )
    db.add(row)
    db.commit()
    return row


def _latest_current_versions(db: Session, project_id: str) -> list[tuple[MigrationArtifact, MigrationArtifactVersion, MigrationObject]]:
    # Defensive de-duplication by source object. Older builds could leave more than one
    # artifact row for an object; deployment must evaluate exactly one current version.
    chosen: dict[str, tuple[MigrationArtifact, MigrationArtifactVersion, MigrationObject]] = {}
    arts = db.scalars(select(MigrationArtifact).where(MigrationArtifact.project_id == project_id)).all()
    for art in arts:
        av = db.scalar(select(MigrationArtifactVersion).where(
            MigrationArtifactVersion.project_id == project_id,
            MigrationArtifactVersion.artifact_id == art.id,
            MigrationArtifactVersion.version == art.current_version,
        ))
        obj = db.get(MigrationObject, art.object_id)
        if not (av and obj and obj.project_id == project_id):
            continue
        prior=chosen.get(obj.id)
        if prior is None or (av.created_at, av.version, av.id) > (prior[1].created_at, prior[1].version, prior[1].id):
            chosen[obj.id]=(art,av,obj)
    return sorted(chosen.values(), key=lambda x:(x[2].schema_name.lower(),x[2].object_name.lower(),x[2].object_type))


def _approved(db: Session, project_id: str, version_id: str) -> bool:
    # Review decisions are immutable audit events. The latest decision is the effective state.
    latest = db.scalars(select(MigrationReview).where(
        MigrationReview.project_id == project_id,
        MigrationReview.artifact_version_id == version_id,
    ).order_by(MigrationReview.reviewed_at.desc())).first()
    return bool(latest and latest.status == "APPROVED")


def _mapping(db: Session, project_id: str, object_id: str, environment: str) -> MigrationMapping | None:
    return db.scalar(select(MigrationMapping).where(
        MigrationMapping.project_id == project_id,
        MigrationMapping.object_id == object_id,
        MigrationMapping.environment == environment.upper(),
    ))


def _parse_target_fqn(fqn: str) -> tuple[str, str, str]:
    parts = [x.strip().strip('`') for x in fqn.split('.')]
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"Target FQN must be catalog.schema.object: {fqn}")
    return parts[0], parts[1], parts[2]


def _safe_create_catalog_and_schema(catalog: str, schema: str) -> None:
    try:
        execute_sql(f"CREATE CATALOG IF NOT EXISTS `{catalog}`", safe_retry=False)
    except Exception:
        pass
    execute_sql(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`", safe_retry=False)


def _expected_schema(db: Session, project_id: str, object_id: str) -> list[dict[str, str]]:
    from app.services.rules import map_sqlserver_type
    cols = db.scalars(select(MigrationColumn).where(
        MigrationColumn.project_id == project_id,
        MigrationColumn.object_id == object_id,
    ).order_by(MigrationColumn.ordinal)).all()
    result = [{"name": c.column_name, "type": map_sqlserver_type(c.data_type, c.precision, c.scale)} for c in cols]
    result += [
        {"name": "_migration_ingested_at", "type": "TIMESTAMP"},
        {"name": "_migration_source_system", "type": "STRING"},
    ]
    return result


def _describe_target(fqn: str) -> list[dict[str, str]] | None:
    try:
        rows = execute_sql(f"DESCRIBE TABLE {fqn}", safe_retry=True)
    except Exception as e:
        text = str(e).lower()
        if any(x in text for x in ("table_or_view_not_found", "not found", "does not exist", "cannot be found")):
            return None
        raise
    actual = []
    for row in rows:
        if not row or not row[0] or str(row[0]).startswith("#"):
            continue
        actual.append({"name": str(row[0]), "type": str(row[1])})
    return actual


def _dependency_order(db: Session, project_id: str, object_ids: set[str]) -> list[str]:
    objects = db.scalars(select(MigrationObject).where(MigrationObject.project_id == project_id)).all()
    lookup: dict[tuple[str, str], str] = {(o.schema_name.lower(), o.object_name.lower()): o.id for o in objects}
    edges: list[tuple[str, str]] = []
    for dep in db.scalars(select(MigrationDependency).where(MigrationDependency.project_id == project_id)).all():
        if dep.object_id not in object_ids or not dep.referenced_object:
            continue
        target_id = None
        if dep.referenced_schema:
            target_id = lookup.get((dep.referenced_schema.lower(), dep.referenced_object.lower()))
        else:
            # Some dependency metadata can omit schema. Resolve only when the object name is
            # unambiguous within the current project; never silently assume dbo or another schema.
            matches = [oid for (schema_name, object_name), oid in lookup.items() if object_name == dep.referenced_object.lower()]
            if len(matches) == 1:
                target_id = matches[0]
        if target_id and target_id in object_ids and target_id != dep.object_id:
            edges.append((dep.object_id, target_id))
    if not edges:
        # Tables before dependent layers gives deterministic stable order when dependency metadata is sparse.
        layer_rank = {"BRONZE": 0, "SILVER": 1, "GOLD": 2}
        objs = [o for o in objects if o.id in object_ids]
        def rank(o):
            c = db.scalar(select(MigrationClassification).where(
                MigrationClassification.project_id == project_id,
                MigrationClassification.object_id == o.id,
            ))
            return (layer_rank.get(c.selected_layer if c else "BRONZE", 9), o.schema_name.lower(), o.object_name.lower())
        return [o.id for o in sorted(objs, key=rank)]
    ordered = topo_order(edges)
    remaining = sorted(object_ids - set(ordered))
    return [x for x in ordered if x in object_ids] + remaining


def dev_precheck(db: Session, project_id: str, test_databricks: bool = True, ignore_deployment_issues: bool = False) -> dict[str, Any]:
    if not db.get(MigrationProject, project_id):
        raise ValueError("Project not found")
    blockers: list[dict[str, str]] = []
    warnings: list[str] = []
    versions = _latest_current_versions(db, project_id)
    if not versions:
        blockers.append({"code": "NO_ARTIFACTS", "message": "No generated artifacts exist for this project."})

    for art, av, obj in versions:
        if not _approved(db, project_id, av.id):
            blockers.append({"code": "APPROVAL", "message": f"{obj.schema_name}.{obj.object_name} v{av.version} is not approved."})
        if av.source_hash != (obj.source_hash or ""):
            blockers.append({"code": "SOURCE_DRIFT", "message": f"{obj.schema_name}.{obj.object_name} source metadata changed after artifact generation."})
        if not _mapping(db, project_id, obj.id, "DEV"):
            blockers.append({"code": "MAPPING", "message": f"DEV mapping missing for {obj.schema_name}.{obj.object_name}."})
        if obj.object_type == "TRIGGER":
            blockers.append({"code": "ARCHITECTURE_REVIEW", "message": f"Trigger {obj.schema_name}.{obj.object_name} cannot be blindly deployed."})
        if any(marker in av.content for marker in NON_EXECUTABLE_MARKERS):
            blockers.append({"code": "NON_EXECUTABLE_ARTIFACT", "message": f"{obj.schema_name}.{obj.object_name} is approved for review but is not an executable Databricks artifact yet."})
        if obj.object_type == "TABLE":
            cols = db.scalars(select(MigrationColumn).where(
                MigrationColumn.project_id == project_id,
                MigrationColumn.object_id == obj.id,
                MigrationColumn.is_computed == False,  # noqa: E712
            ).order_by(MigrationColumn.ordinal)).all()
            for item in transport_contract(cols):
                if item["review_required"]:
                    warnings.append(
                        f"{obj.schema_name}.{obj.object_name}.{item['column']}: {item['source_type']} uses "
                        f"{item['strategy']} ({item['notes']})"
                    )

    _resolve_stale_ownership_issues(db, project_id)
    open_blockers = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if ignore_deployment_issues:
        open_blockers = [x for x in open_blockers if x.issue_type != "DEPLOYMENT"]
    if open_blockers:
        blockers.append({"code": "OPEN_ISSUES", "message": f"{len(open_blockers)} open blocker issue(s) must be resolved."})

    dbx = {"tested": False, "ok": None}
    if test_databricks:
        try:
            rows = execute_sql("SELECT current_catalog(), current_schema(), current_user()", safe_retry=True)
            dbx = {"tested": True, "ok": True, "result": [list(r) for r in rows]}
        except Exception as e:
            blockers.append({"code": "DATABRICKS_CONNECTIVITY", "message": str(e)})
            dbx = {"tested": True, "ok": False, "error": str(e)}

    # Stable, unique blocker list prevents repeated messages for the same object/rule.
    unique_blockers=[]
    seen=set()
    for item in blockers:
        key=(item.get("code"),item.get("message"))
        if key not in seen:
            seen.add(key); unique_blockers.append(item)
    blockers=unique_blockers
    result = {
        "eligible": not blockers,
        "environment": "DEV",
        "approved_artifacts": len(versions) - sum(1 for _, av, _ in versions if not _approved(db, project_id, av.id)),
        "artifact_count": len(versions),
        "blockers": blockers,
        "warnings": warnings,
        "databricks": dbx,
    }
    _validation_evidence(db, project_id, "PRECHECK", "PASSED" if result["eligible"] else "BLOCKED", validation_type="DEV_PRECHECK", details=result)
    return result


def _safe_execute_artifact(content: str, *, allow_destructive: bool = False) -> None:
    # One artifact version is treated as a single governed statement/batch. Destructive
    # statements require the explicit, per-request DEV approval supplied by the operator.
    upper = content.upper()
    forbidden = ("DROP TABLE", "DROP VIEW", "DROP SCHEMA", "TRUNCATE TABLE", "DELETE FROM")
    if any(x in upper for x in forbidden) and not allow_destructive:
        raise RuntimeError("Generated artifact contains a destructive statement; explicit governed replacement is required.")
    execute_sql(content, safe_retry=False)


def _apply_table_schema_policy(db: Session, project_id: str, obj: MigrationObject, mapping: MigrationMapping,
                               allow_destructive: bool) -> dict[str, Any]:
    cfg = get_settings()
    actual = _describe_target(mapping.target_fqn)
    if actual is None:
        return {"action": "CREATE", "schema_status": "MISSING"}
    expected = _expected_schema(db, project_id, obj.id)
    drift = compare_schema(expected, actual)
    if drift["status"] == "IDENTICAL":
        return {"action": "REUSE", "schema_status": "IDENTICAL", "drift": drift}
    if drift["status"] == "NON_BREAKING_CHANGE" and cfg.deployment_policy in {"ALTER_SAFE_CHANGES", "REPLACE_IN_DEV"}:
        existing = {x["name"].lower() for x in actual}
        for col in expected:
            if col["name"].lower() not in existing:
                execute_sql(f"ALTER TABLE {mapping.target_fqn} ADD COLUMN `{col['name']}` {col['type']}", safe_retry=False)
        return {"action": "ALTER_SAFE", "schema_status": drift["status"], "drift": drift}
    if cfg.dev_schema_policy.upper() == "REPLACE_CHANGED" and allow_destructive:
        return {"action": "REPLACE", "schema_status": drift["status"], "drift": drift}
    raise RuntimeError(f"Target schema drift is {drift['status']}; destructive replacement is blocked without explicit approval.")


def _source_connection_string(src: MigrationSource) -> str:
    cfg = get_settings()
    st = (cfg.source_type or "ORACLE").upper()
    if st == "ORACLE" or (cfg.oracle_host and not cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
        host = cfg.oracle_host or src.server_name or "localhost"
        port = cfg.oracle_port or 1521
        db = src.database_name or cfg.oracle_service_name or cfg.oracle_sid or "ORCLPDB1"
        user = cfg.oracle_username or "system"
        pwd = cfg.oracle_password or ""
        return f"oracle://{user}:{pwd}@{host}:{port}/{db}"
    elif st == "MYSQL" or cfg.mysql_host:
        host = cfg.mysql_host or src.server_name or "localhost"
        port = cfg.mysql_port or 3306
        db = src.database_name or cfg.mysql_database or ""
        user = cfg.mysql_username or "root"
        pwd = cfg.mysql_password or ""
        return f"mysql://{user}:{pwd}@{host}:{port}/{db}"
    elif st in {"POSTGRESQL", "POSTGRES"} or (src.server_name and ":" in src.server_name) or cfg.postgres_host:
        host = cfg.postgres_host or src.server_name or "localhost"
        port = cfg.postgres_port or 5432
        db = src.database_name or cfg.postgres_database or "postgres"
        user = cfg.postgres_username or "postgres"
        pwd = cfg.postgres_password or ""
        sslmode = cfg.postgres_sslmode or "prefer"
        return f"postgresql://{user}:{pwd}@{host}:{port}/{db}?sslmode={sslmode}"
    driver = cfg.sqlserver_driver.replace("{", "").replace("}", "")
    if cfg.sqlserver_username:
        return (f"DRIVER={{{driver}}};SERVER={src.server_name};DATABASE={src.database_name};"
                f"UID={cfg.sqlserver_username};PWD={cfg.sqlserver_password or ''};TrustServerCertificate=yes;")
    return (f"DRIVER={{{driver}}};SERVER={src.server_name};DATABASE={src.database_name};"
            "Trusted_Connection=yes;TrustServerCertificate=yes;")


@contextmanager
def _source_rows(src, obj, cols, sql_text, max_rows):
    if connector_info(src.id)["mode"] == "CONNECTOR":
        with TableStream(src.id, obj, cols, max_rows) as stream:
            yield stream
    else:
        cfg = get_settings()
        st = (cfg.source_type or "ORACLE").upper()
        if st == "ORACLE" or (cfg.oracle_host and not cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
            from app.services.discovery import parse_oracle_conn, _get_oracle_connection
            conn_url = _source_connection_string(src)
            conn_dict = parse_oracle_conn(conn_url)
            conn = _get_oracle_connection(conn_dict)
            try:
                cursor = conn.cursor()
                cursor.execute(sql_text)
                yield cursor
            finally:
                if hasattr(conn, "close"):
                    conn.close()
        elif st == "MYSQL" or (cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
            from app.services.discovery import parse_mysql_conn, _get_mysql_connection
            conn_url = _source_connection_string(src)
            conn_dict = parse_mysql_conn(conn_url)
            conn = _get_mysql_connection(conn_dict)
            try:
                cursor = conn.cursor()
                cursor.execute(sql_text)
                yield cursor
            finally:
                if hasattr(conn, "close"):
                    conn.close()
        elif st in {"POSTGRESQL", "POSTGRES"} or (cfg.postgres_host and not cfg.sqlserver_host):
            try:
                import psycopg2
            except ImportError:
                import psycopg as psycopg2
            conn_url = _source_connection_string(src)
            from app.services.discovery import parse_postgres_conn
            conn_dict = parse_postgres_conn(conn_url)
            conn = psycopg2.connect(**conn_dict)
            try:
                cursor = conn.cursor()
                cursor.execute(sql_text)
                yield cursor
            finally:
                if hasattr(conn, "close"):
                    conn.close()
        else:
            import pyodbc
            with pyodbc.connect(_source_connection_string(src), timeout=30) as conn:
                cursor = conn.cursor()
                cursor.execute(sql_text)
                yield cursor


def load_bronze_table(db: Session, project_id: str, obj: MigrationObject, mapping: MigrationMapping,
                      run_id: str, batch_size: int, max_rows: int | None, load_mode: str,
                      replace_existing_data: bool) -> dict[str, Any]:
    if obj.object_type != "TABLE" or mapping.target_layer != "BRONZE":
        return {"status": "SKIPPED", "rows": 0, "reason": "Not a Bronze table"}
    if load_mode.upper() not in {"FULL_LOAD", "APPEND"}:
        raise RuntimeError(f"Load mode {load_mode} requires watermark/CDC metadata not configured for this object.")
    src = db.get(MigrationSource, obj.source_id)
    if not src or src.project_id != project_id:
        raise RuntimeError("Source profile not found for Bronze load")
    if connector_info(src.id)["mode"] == "CONNECTOR":
        connector_request(src.id, "test")
    if load_mode.upper() == "FULL_LOAD":
        existing = execute_sql(f"SELECT COUNT(*) FROM {mapping.target_fqn}", safe_retry=True)
        existing_count = int(existing[0][0]) if existing else 0
        if existing_count and not replace_existing_data:
            raise RuntimeError(f"Target {mapping.target_fqn} already contains {existing_count} rows. FULL_LOAD replacement requires explicit approval.")
        if existing_count and replace_existing_data:
            execute_sql(f"TRUNCATE TABLE {mapping.target_fqn}", safe_retry=False)

    cols = db.scalars(select(MigrationColumn).where(
        MigrationColumn.project_id == project_id,
        MigrationColumn.object_id == obj.id,
        MigrationColumn.is_computed == False,  # noqa: E712
    ).order_by(MigrationColumn.ordinal)).all()
    names = [c.column_name for c in cols]
    if not names:
        return {"status": "PASSED", "rows": 0}

    cfg = get_settings()
    st = (cfg.source_type or "ORACLE").upper()
    is_oracle = st == "ORACLE" or (cfg.oracle_host and not cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host)
    is_mysql = st == "MYSQL" or (cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host and not cfg.oracle_host)
    is_pg = st in {"POSTGRESQL", "POSTGRES"} or (cfg.postgres_host and not cfg.sqlserver_host and not cfg.mysql_host and not cfg.oracle_host)
    source_type_label = "ORACLE" if is_oracle else ("MYSQL" if is_mysql else ("POSTGRESQL" if is_pg else "SQLSERVER"))
    select_cols = ",".join(source_select_expression(c, source_type_label) for c in cols)
    if is_oracle:
        clean_schema = obj.schema_name.replace('"', '""') if obj.schema_name else ""
        clean_obj = obj.object_name.replace('"', '""')
        source_table = f'"{clean_schema}"."{clean_obj}"' if clean_schema else f'"{clean_obj}"'
        sql_text = f"SELECT {select_cols} FROM {source_table}"
        if max_rows:
            sql_text += f" FETCH FIRST {int(max_rows)} ROWS ONLY"
    elif is_mysql:
        clean_schema = obj.schema_name.replace('`', '``') if obj.schema_name else ""
        clean_obj = obj.object_name.replace('`', '``')
        source_table = f"`{clean_schema}`.`{clean_obj}`" if clean_schema else f"`{clean_obj}`"
        sql_text = f"SELECT {select_cols} FROM {source_table}"
        if max_rows:
            sql_text += f" LIMIT {int(max_rows)}"
    elif is_pg:
        clean_schema = obj.schema_name.replace('"', '""')
        clean_obj = obj.object_name.replace('"', '""')
        source_table = f'"{clean_schema}"."{clean_obj}"'
        sql_text = f"SELECT {select_cols} FROM {source_table}"
        if max_rows:
            sql_text += f" LIMIT {int(max_rows)}"
    else:
        source_table = f"[{obj.schema_name.replace(']',']]')}].[{obj.object_name.replace(']',']]')}]"
        sql_text = f"SELECT {select_cols} FROM {source_table}"
        if max_rows:
            sql_text = f"SELECT TOP ({int(max_rows)}) {select_cols} FROM {source_table}"
    value_expressions = [target_parameter_expression(c) for c in cols] + ["?"]
    placeholders = ",".join(value_expressions)
    target_cols = ",".join([f"`{n.replace('`','``')}`" for n in names] + ["`_migration_source_system`"])
    insert_sql = f"INSERT INTO {mapping.target_fqn} ({target_cols}, `_migration_ingested_at`) VALUES ({placeholders}, current_timestamp())"
    contract = transport_contract(cols)
    summary = transport_summary(cols)
    rows_loaded = 0
    with _source_rows(src, obj, cols, sql_text, max_rows) as scur, databricks_connection() as target_conn:
        tcur = target_conn.cursor()
        while True:
            batch = scur.fetchmany(batch_size)
            if not batch:
                break
            payload = []
            batch_start = rows_loaded + 1
            for offset, source_row in enumerate(batch):
                # Normalize every value before any target write. This makes runtime type
                # failures deterministic, column-aware and safe to resume because the
                # bad batch is rejected before executemany is invoked.
                normalized = normalize_row(cols, tuple(source_row), row_index=batch_start + offset)
                payload.append(normalized + (f"{src.server_name}/{src.database_name}",))
            tcur.executemany(insert_sql, payload)
            rows_loaded += len(payload)
    _record_step(
        db, project_id, run_id, "BRONZE_LOAD", "PASSED", obj.id,
        rows_loaded=rows_loaded, target=mapping.target_fqn, transport=summary,
    )
    return {"status": "PASSED", "rows": rows_loaded, "transport": summary, "contract": contract}


def deploy_dev(db: Session, project_id: str, *, allow_destructive: bool = False,
               batch_size: int | None = None, max_rows: int | None = None,
               load_mode: str | None = None, replace_existing_data: bool = False,
               resume_run_id: str | None = None) -> dict[str, Any]:
    cfg = get_settings()
    pre = dev_precheck(db, project_id, test_databricks=True, ignore_deployment_issues=bool(resume_run_id))
    if not pre["eligible"]:
        raise ValueError("DEV precheck blocked deployment: " + "; ".join(x["message"] for x in pre["blockers"]))

    if resume_run_id:
        run = db.get(MigrationRun, resume_run_id)
        if not run or run.project_id != project_id or run.environment != "DEV":
            raise ValueError("Resume run not found in project")
        if run.status not in {"FAILED", "BLOCKED"}:
            raise ValueError("Only FAILED/BLOCKED DEV runs can be resumed")
        run.status = "RUNNING"; run.ended_at = None
        run_id = run.id
    else:
        run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="DEV_DEPLOYMENT", environment="DEV", status="RUNNING")
        db.add(run); db.commit(); run_id = run.id

    _deployment_evidence(db, project_id, run_id, "RUNNING", action="DEPLOY_APPROVED", destructive_approved=allow_destructive)
    versions = _latest_current_versions(db, project_id)
    object_ids = {obj.id for _, _, obj in versions}
    order = _dependency_order(db, project_id, object_ids)
    index = {obj.id: (art, av, obj) for art, av, obj in versions}

    completed_ids: set[str] = set()
    if resume_run_id:
        prior = db.scalars(select(MigrationDeployment).where(
            MigrationDeployment.project_id == project_id,
            MigrationDeployment.environment == "DEV",
            MigrationDeployment.status == "PASSED",
        )).all()
        for row in prior:
            p = _payload(row)
            if p.get("run_id") == run_id and row.object_id:
                completed_ids.add(row.object_id)

    results = []
    failed_object = None
    failure_stage = "DEV_DEPLOYMENT"
    try:
        for object_id in order:
            art, av, obj = index[object_id]
            failed_object = f"{obj.schema_name}.{obj.object_name}"
            failure_stage = "MAPPING"
            mapping = _mapping(db, project_id, object_id, "DEV")
            if not mapping:
                raise RuntimeError(f"DEV mapping missing for {obj.schema_name}.{obj.object_name}")
            # Prevent replacement only when another project owns this target in the
            # same Databricks workspace. Identical FQNs in separate accounts are safe.
            owner = _target_owner_collision(db, project_id, mapping.target_fqn)
            if owner:
                raise RuntimeError(
                    f"Target ownership collision: {mapping.target_fqn} is already owned "
                    f"by project {owner.project_id} in workspace {databricks_workspace_identity()}"
                )
            catalog, schema, _ = _parse_target_fqn(mapping.target_fqn)
            failure_stage = "TARGET_SCHEMA"
            _safe_create_catalog_and_schema(catalog, schema)
            if object_id in completed_ids:
                results.append({"object_id": object_id, "name": obj.object_name, "status": "SKIPPED_ALREADY_PASSED"})
                continue
            _record_step(db, project_id, run_id, "DEPLOY_OBJECT", "RUNNING", object_id, target=mapping.target_fqn, artifact_version=av.version)
            failure_stage = "TARGET_SCHEMA_PREFLIGHT"
            schema_action = None
            if obj.object_type == "TABLE":
                schema_action = _apply_table_schema_policy(db, project_id, obj, mapping, allow_destructive)
                if schema_action["action"] == "CREATE":
                    failure_stage = "DEPLOY_DDL"
                    _safe_execute_artifact(av.content, allow_destructive=allow_destructive)
                elif schema_action["action"] == "REPLACE":
                    failure_stage = "GOVERNED_DEV_REPLACE"
                    execute_sql(f"DROP TABLE {mapping.target_fqn}", safe_retry=False)
                    failure_stage = "DEPLOY_DDL"
                    _safe_execute_artifact(av.content, allow_destructive=allow_destructive)
            else:
                # CREATE OR REPLACE is safe for views/functions when already generated that way; raw destructive SQL remains blocked.
                failure_stage = "DEPLOY_DDL"
                _safe_execute_artifact(av.content, allow_destructive=allow_destructive)

            failure_stage = "BRONZE_LOAD"
            load_result = load_bronze_table(
                db, project_id, obj, mapping, run_id,
                batch_size or cfg.batch_size, max_rows if max_rows is not None else cfg.max_rows,
                load_mode or cfg.load_mode, replace_existing_data,
            )
            _deployment_evidence(db, project_id, run_id, "PASSED", object_id=object_id,
                                 target_fqn=mapping.target_fqn, artifact_version_id=av.id,
                                 artifact_version=av.version, layer=mapping.target_layer,
                                 schema_action=schema_action, load=load_result)
            _record_step(db, project_id, run_id, "DEPLOY_OBJECT", "PASSED", object_id, target=mapping.target_fqn)
            for issue in db.scalars(select(MigrationIssue).where(MigrationIssue.project_id==project_id, MigrationIssue.object_id==object_id, MigrationIssue.issue_type=="DEPLOYMENT", MigrationIssue.status=="OPEN")).all():
                issue.status="RESOLVED"
            db.commit()
            results.append({"object_id": object_id, "name": obj.object_name, "status": "PASSED", "layer": mapping.target_layer, "load": load_result})

        run.status = "PASSED"; run.ended_at = datetime.utcnow(); run.checkpoint = "DEV_DEPLOYMENT_COMPLETE"
        db.commit()
        _deployment_evidence(db, project_id, run_id, "PASSED", deployed=sum(1 for x in results if x["status"] == "PASSED"), failed=0)
        return {"run_id": run_id, "status": "PASSED", "results": results, "failed_object": None}
    except Exception as e:
        classified = classify_execution_error(e, failure_stage)
        run.status = "FAILED"; run.ended_at = datetime.utcnow(); run.checkpoint = failed_object
        db.commit()
        _deployment_evidence(
            db, project_id, run_id, "FAILED", failed_object=failed_object, error=str(e),
            error_category=classified["error_category"], error_code=classified["error_code"],
            failure_stage=failure_stage, retryable=classified["retryable"],
            deterministic_remediation_available=classified["deterministic_remediation_available"],
            remediation=classified["recommended_action"],
        )
        if failed_object:
            obj = next((o for _, _, o in versions if f"{o.schema_name}.{o.object_name}" == failed_object), None)
            existing=db.scalars(select(MigrationIssue).where(
                MigrationIssue.project_id==project_id, MigrationIssue.object_id==(obj.id if obj else None),
                MigrationIssue.issue_type=="DEPLOYMENT", MigrationIssue.status=="OPEN"
            )).first()
            details=json.dumps({
                "run_id":run_id, "failed_object":failed_object, "error":str(e),
                "failure_stage":failure_stage, **classified,
            }, default=str)
            if existing:
                existing.message=f"DEV deployment failed at {failed_object}"; existing.technical_details=details
                existing.recommended_action=classified["recommended_action"]
            else:
                db.add(MigrationIssue(id=uid("ISS"), project_id=project_id, object_id=obj.id if obj else None,
                                      issue_type="DEPLOYMENT", severity="BLOCKER", message=f"DEV deployment failed at {failed_object}",
                                      technical_details=details, recommended_action=classified["recommended_action"]))
            db.commit()
        return {
            "run_id": run_id, "status": "FAILED", "results": results, "failed_object": failed_object,
            "error": str(e), "failure": classified,
        }


def latest_failed_dev_run(db: Session, project_id: str) -> MigrationRun | None:
    return db.scalars(select(MigrationRun).where(
        MigrationRun.project_id == project_id,
        MigrationRun.environment == "DEV",
        MigrationRun.stage == "DEV_DEPLOYMENT",
        MigrationRun.status.in_(["FAILED", "BLOCKED"]),
    ).order_by(MigrationRun.started_at.desc())).first()


def _latest_successful_medallion_run(
    db: Session, project_id: str, environment: str
) -> tuple[str, list[tuple[MigrationDeployment, dict[str, Any]]]] | None:
    """Return the latest complete Medallion run from immutable deployment evidence.

    The deployed version IDs recorded on MigrationDeployment are the authoritative manifest.
    DEV uses MDR run IDs while promoted environments use governed MigrationRun IDs; the
    explicit Medallion completion marker identifies both without relying on an ID prefix.
    """
    rows = list(db.scalars(select(MigrationDeployment).where(
        MigrationDeployment.project_id == project_id,
        MigrationDeployment.environment == environment,
    ).order_by(MigrationDeployment.created_at.desc())).all())
    grouped: dict[str, list[tuple[MigrationDeployment, dict[str, Any]]]] = {}
    newest: dict[str, datetime] = {}
    completed: set[str] = set()
    for row in rows:
        payload = _payload(row)
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        if payload.get("medallion_run_complete") and row.status == "PASSED":
            completed.add(run_id)
            newest[run_id] = max(newest.get(run_id, row.created_at), row.created_at)
            continue
        if not payload.get("medallion_node_id"):
            continue
        grouped.setdefault(run_id, []).append((row, payload))
        newest[run_id] = max(newest.get(run_id, row.created_at), row.created_at)
    expected_nodes = {node.id for node in db.scalars(select(MigrationMedallionNode).where(
        MigrationMedallionNode.project_id == project_id,
        MigrationMedallionNode.environment == environment,
        MigrationMedallionNode.layer.in_(["BRONZE", "SILVER", "GOLD"]),
    )).all()}
    for run_id in sorted(grouped, key=lambda rid: newest[rid], reverse=True):
        run_rows = grouped[run_id]
        if run_rows and all(row.status == "PASSED" for row, _ in run_rows):
            # One deployed artifact per node is the exact immutable manifest for this run.
            chosen: dict[str, tuple[MigrationDeployment, dict[str, Any]]] = {}
            for row, payload in run_rows:
                node_id = str(payload["medallion_node_id"])
                prior = chosen.get(node_id)
                if prior is None or row.created_at > prior[0].created_at:
                    chosen[node_id] = (row, payload)
            # New builds record an explicit completion marker. Older builds are accepted only
            # when their evidence covers the entire current Medallion plan.
            if run_id in completed or (expected_nodes and set(chosen) == expected_nodes):
                return run_id, list(chosen.values())
    return None


def medallion_deployment_status(
    db: Session, project_id: str, environment: str = "DEV"
) -> dict[str, Any]:
    """Return read-only evidence for the latest complete Medallion deployment."""
    env = environment.upper()
    deployment = _latest_successful_medallion_run(db, project_id, env)
    if not deployment:
        return {
            "environment": env,
            "status": "NOT_STARTED",
            "run_id": None,
            "deployed": 0,
        }
    run_id, manifest = deployment
    return {
        "environment": env,
        "status": "PASSED",
        "run_id": run_id,
        "deployed": len(manifest),
    }


def _routine_exists(target_fqn: str, routine_type: str) -> None:
    """Validate routine metadata without invoking business logic or causing side effects."""
    statement = (
        f"DESCRIBE FUNCTION EXTENDED {target_fqn}"
        if routine_type == "FUNCTION"
        else f"DESCRIBE PROCEDURE {target_fqn}"
    )
    execute_sql(statement, safe_retry=True)


def _source_table_count(source: MigrationSource, obj: MigrationObject) -> int:
    if connector_info(source.id)["mode"] == "CONNECTOR":
        return int(connector_request(source.id, "count", {"schema": obj.schema_name, "table": obj.object_name})["count"])
    cfg = get_settings()
    st = (cfg.source_type or "ORACLE").upper()
    if st == "ORACLE" or (cfg.oracle_host and not cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
        from app.services.discovery import parse_oracle_conn, _get_oracle_connection
        conn_url = _source_connection_string(source)
        conn_dict = parse_oracle_conn(conn_url)
        conn = _get_oracle_connection(conn_dict)
        try:
            cur = conn.cursor()
            clean_schema = obj.schema_name.replace('"', '""') if obj.schema_name else ""
            clean_obj = obj.object_name.replace('"', '""')
            tbl_name = f'"{clean_schema}"."{clean_obj}"' if clean_schema else f'"{clean_obj}"'
            cur.execute(f"SELECT COUNT(*) FROM {tbl_name}")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    elif st == "MYSQL" or (cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
        from app.services.discovery import parse_mysql_conn, _get_mysql_connection
        conn_url = _source_connection_string(source)
        conn_dict = parse_mysql_conn(conn_url)
        conn = _get_mysql_connection(conn_dict)
        try:
            cur = conn.cursor()
            clean_schema = obj.schema_name.replace('`', '``') if obj.schema_name else ""
            clean_obj = obj.object_name.replace('`', '``')
            tbl_name = f"`{clean_schema}`.`{clean_obj}`" if clean_schema else f"`{clean_obj}`"
            cur.execute(f"SELECT COUNT(*) FROM {tbl_name}")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    elif st in {"POSTGRESQL", "POSTGRES"} or (cfg.postgres_host and not cfg.sqlserver_host):
        try:
            import psycopg2
        except ImportError:
            import psycopg as psycopg2
        conn_url = _source_connection_string(source)
        from app.services.discovery import parse_postgres_conn
        conn_dict = parse_postgres_conn(conn_url)
        conn = psycopg2.connect(**conn_dict)
        try:
            cur = conn.cursor()
            clean_schema = obj.schema_name.replace('"', '""')
            clean_obj = obj.object_name.replace('"', '""')
            cur.execute(f'SELECT COUNT(*) FROM "{clean_schema}"."{clean_obj}"')
            return int(cur.fetchone()[0])
        finally:
            conn.close()
    else:
        import pyodbc
        with pyodbc.connect(_source_connection_string(source), timeout=30) as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) FROM [{obj.schema_name.replace(']', ']]')}]."
                f"[{obj.object_name.replace(']', ']]')}]"
            )
            return int(cur.fetchone()[0])


def _reconcile_medallion_run(
    db: Session,
    project_id: str,
    environment: str,
    run_id: str,
    manifest: list[tuple[MigrationDeployment, dict[str, Any]]],
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    passed = failed = 0
    for evidence, deployed in sorted(
        manifest,
        key=lambda item: ({"BRONZE": 1, "SILVER": 2, "GOLD": 3}.get(str(item[1].get("layer")), 9),
                          str(item[1].get("target_fqn") or "").lower()),
    ):
        node = db.get(MigrationMedallionNode, deployed.get("medallion_node_id"))
        version = db.get(MigrationStageArtifactVersion, deployed.get("artifact_version_id"))
        obj = db.get(MigrationObject, node.source_object_id) if node and node.source_object_id else None
        target_fqn = str(deployed.get("target_fqn") or (node.target_fqn if node else ""))
        node_type = str(node.node_type if node else "UNKNOWN").upper()
        source_type = str(obj.object_type if obj else "").upper()
        status = "PASSED"
        source_count = target_count = None
        error = None
        check_type = "TARGET_EXISTENCE_COUNT"
        try:
            if node_type in {"SQL_FUNCTION", "FUNCTION_PLAN"} or source_type == "FUNCTION":
                check_type = "FUNCTION_EXISTENCE"
                _routine_exists(target_fqn, "FUNCTION")
            elif node_type in {"SQL_PROCEDURE", "ROUTINE_PLAN"} or source_type == "PROCEDURE":
                check_type = "PROCEDURE_EXISTENCE"
                _routine_exists(target_fqn, "PROCEDURE")
            else:
                target_rows = execute_sql(f"SELECT COUNT(*) FROM {target_fqn}", safe_retry=True)
                target_count = int(target_rows[0][0]) if target_rows else 0
                if node and node.layer == "BRONZE" and obj and source_type == "TABLE":
                    check_type = "ROW_COUNT"
                    src = db.get(MigrationSource, obj.source_id)
                    if not src:
                        raise RuntimeError("Source profile missing")
                    source_count = _source_table_count(src, obj)
                    if source_count != target_count:
                        status = "FAILED"
        except Exception as exc:
            status = "FAILED"
            error = str(exc)
        passed += status == "PASSED"
        failed += status == "FAILED"
        item = {
            "object_id": obj.id if obj else evidence.object_id,
            "medallion_node_id": node.id if node else deployed.get("medallion_node_id"),
            "artifact_version_id": version.id if version else deployed.get("artifact_version_id"),
            "artifact_version": version.version if version else None,
            "object": f"{obj.schema_name}.{obj.object_name}" if obj else (node.target_name if node else target_fqn),
            "target_fqn": target_fqn,
            "layer": deployed.get("layer") or (node.layer if node else None),
            "object_type": node_type,
            "reconciliation_type": check_type,
            "source_count": source_count,
            "target_count": target_count,
            "status": status,
            "error": error,
        }
        details.append(item)
        db.add(MigrationReconciliationDetail(
            id=uid("RCD"), project_id=project_id, object_id=item["object_id"],
            environment=environment, status=status, payload_json=_json({"run_id": run_id, **item}),
        ))
    overall = "PASSED" if failed == 0 and details else "FAILED"
    summary = {
        "run_id": run_id, "workflow": "MEDALLION", "passed": passed,
        "failed": failed, "details_count": len(details),
    }
    db.add(MigrationReconciliation(
        id=uid("REC"), project_id=project_id, environment=environment,
        status=overall, payload_json=_json(summary),
    ))
    db.commit()
    return {**summary, "status": overall, "details": details}


def run_reconciliation(db: Session, project_id: str, environment: str = "DEV") -> dict[str, Any]:
    env = environment.upper()
    medallion = _latest_successful_medallion_run(db, project_id, env)
    if medallion:
        return _reconcile_medallion_run(db, project_id, env, medallion[0], medallion[1])

    # Backward-compatible fallback for projects that only use the legacy artifact workflow.
    run = db.scalars(select(MigrationRun).where(
        MigrationRun.project_id == project_id,
        MigrationRun.environment == env,
        MigrationRun.stage == "DEV_DEPLOYMENT",
        MigrationRun.status == "PASSED",
    ).order_by(MigrationRun.started_at.desc())).first()
    if not run:
        raise ValueError(f"No PASSED {env} deployment run exists")

    objects = db.scalars(select(MigrationObject).where(MigrationObject.project_id == project_id)).all()
    details = []
    passed = failed = 0
    for obj in objects:
        mapping = _mapping(db, project_id, obj.id, env)
        if not mapping:
            continue
        status = "PASSED"; source_count = target_count = None; error = None
        try:
            target_rows = execute_sql(f"SELECT COUNT(*) FROM {mapping.target_fqn}", safe_retry=True)
            target_count = int(target_rows[0][0]) if target_rows else 0
            if obj.object_type == "TABLE":
                src = db.get(MigrationSource, obj.source_id)
                if not src:
                    raise RuntimeError("Source profile missing")
                source_count = _source_table_count(src, obj)
                if source_count != target_count:
                    status = "FAILED"
        except Exception as e:
            status = "FAILED"; error = str(e)
        if status == "PASSED": passed += 1
        else: failed += 1
        item = {"object_id": obj.id, "object": f"{obj.schema_name}.{obj.object_name}", "layer": mapping.target_layer,
                "reconciliation_type": "ROW_COUNT" if obj.object_type == "TABLE" else "TARGET_EXISTENCE_COUNT",
                "source_count": source_count, "target_count": target_count, "status": status, "error": error}
        details.append(item)
        db.add(MigrationReconciliationDetail(id=uid("RCD"), project_id=project_id, object_id=obj.id,
                                             environment=env, status=status, payload_json=_json({"run_id": run.id, **item})))
    overall = "PASSED" if failed == 0 and details else "FAILED"
    db.add(MigrationReconciliation(id=uid("REC"), project_id=project_id, environment=env, status=overall,
                                   payload_json=_json({"run_id": run.id, "passed": passed, "failed": failed, "details_count": len(details)})))
    db.commit()
    return {"run_id": run.id, "status": overall, "passed": passed, "failed": failed, "details": details}


def latest_reconciliation(db: Session, project_id: str, environment: str = "DEV") -> dict[str, Any] | None:
    env = environment.upper()
    row = db.scalars(select(MigrationReconciliation).where(
        MigrationReconciliation.project_id == project_id,
        MigrationReconciliation.environment == env,
    ).order_by(MigrationReconciliation.created_at.desc())).first()
    if not row:
        return None
    summary = _payload(row)
    run_id = summary.get("run_id")
    detail_rows = list(db.scalars(select(MigrationReconciliationDetail).where(
        MigrationReconciliationDetail.project_id == project_id,
        MigrationReconciliationDetail.environment == env,
    ).order_by(MigrationReconciliationDetail.created_at.asc())).all())
    details = [_payload(item) for item in detail_rows if _payload(item).get("run_id") == run_id]
    return {**summary, "status": row.status, "reconciliation_id": row.id,
            "created_at": row.created_at, "details": details}


def evaluate_dev_gate(db: Session, project_id: str) -> dict[str, Any]:
    blockers: list[str] = []
    medallion = _latest_successful_medallion_run(db, project_id, "DEV")
    deployment = db.scalars(select(MigrationRun).where(
        MigrationRun.project_id == project_id, MigrationRun.environment == "DEV",
        MigrationRun.stage == "DEV_DEPLOYMENT",
    ).order_by(MigrationRun.started_at.desc())).first()
    deployment_run_id = medallion[0] if medallion else (deployment.id if deployment else None)
    if not medallion and (not deployment or deployment.status != "PASSED"):
        blockers.append("Latest DEV deployment is not PASSED")

    if medallion:
        for _, payload in medallion[1]:
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not version or version.review_status != "APPROVED" or version.validation_status != "PASSED":
                blockers.append(f"{payload.get('target_fqn')} deployed Medallion version is no longer approved/validated")
    else:
        versions = _latest_current_versions(db, project_id)
        for _, av, obj in versions:
            if not _approved(db, project_id, av.id):
                blockers.append(f"{obj.schema_name}.{obj.object_name} current artifact version is not approved")

    open_issues = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id, MigrationIssue.status == "OPEN", MigrationIssue.severity == "BLOCKER"
    )).all()
    relevant_issues = []
    for issue in open_issues:
        if medallion and issue.issue_type == "DEPLOYMENT":
            try:
                issue_run_id = json.loads(issue.technical_details or "{}").get("run_id")
            except Exception:
                issue_run_id = None
            if issue_run_id and issue_run_id != deployment_run_id:
                continue
        relevant_issues.append(issue)
    if relevant_issues:
        blockers.append(f"{len(relevant_issues)} open blocker issue(s)")

    recon = db.scalars(select(MigrationReconciliation).where(
        MigrationReconciliation.project_id == project_id, MigrationReconciliation.environment == "DEV"
    ).order_by(MigrationReconciliation.created_at.desc())).first()
    if not recon or recon.status != "PASSED":
        blockers.append("Latest DEV reconciliation is not PASSED")

    status = "PASSED" if not blockers else "BLOCKED"
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="QUALITY_GATE", environment="DEV",
                       status=status, ended_at=datetime.utcnow())
    db.add(run); db.flush()
    gate = MigrationQualityGate(id=uid("GAT"), project_id=project_id, run_id=run.id, environment="DEV", status=status,
                                pass_count=3 if status == "PASSED" else max(0, 3-len(blockers)),
                                fail_count=len(blockers), blocker_count=len(blockers),
                                deployment_version=deployment_run_id)
    db.add(gate); db.commit()
    return {"gate_id": gate.id, "run_id": run.id, "status": status, "blockers": blockers,
            "deployment_run_id": deployment_run_id}


def test_promotion_precheck(db: Session, project_id: str, test_databricks: bool = True) -> dict[str, Any]:
    """Validate that the exact successful DEV Medallion manifest can enter TEST."""
    if not db.get(MigrationProject, project_id):
        raise ValueError("Project not found")
    blockers: list[dict[str, str]] = []
    dev_gate = db.scalars(select(MigrationQualityGate).where(
        MigrationQualityGate.project_id == project_id,
        MigrationQualityGate.environment == "DEV",
    ).order_by(MigrationQualityGate.created_at.desc())).first()
    if not dev_gate or dev_gate.status != "PASSED":
        blockers.append({"code": "DEV_GATE", "message": "Latest DEV quality gate is not PASSED."})

    manifest = _latest_successful_medallion_run(db, project_id, "DEV")
    if not manifest:
        blockers.append({"code": "DEV_MANIFEST", "message": "No complete successful DEV Medallion deployment exists."})
    else:
        for _, payload in manifest[1]:
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not version or not version.executable or version.validation_status != "PASSED" or version.review_status != "APPROVED":
                blockers.append({
                    "code": "ARTIFACT_VERSION",
                    "message": f"{payload.get('target_fqn')} DEV artifact version is not approved, executable and validated.",
                })

    open_blockers = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_blockers:
        blockers.append({"code": "OPEN_ISSUES", "message": f"{len(open_blockers)} open blocker issue(s) must be resolved."})

    dbx = {"tested": False, "ok": None}
    if test_databricks:
        try:
            rows = execute_sql("SELECT current_catalog(), current_schema(), current_user()", safe_retry=True)
            dbx = {"tested": True, "ok": True, "result": [list(row) for row in rows]}
        except Exception as exc:
            blockers.append({"code": "DATABRICKS_CONNECTIVITY", "message": str(exc)})
            dbx = {"tested": True, "ok": False, "error": str(exc)}

    result = {
        "eligible": not blockers,
        "source_environment": "DEV",
        "target_environment": "TEST",
        "artifact_count": len(manifest[1]) if manifest else 0,
        "source_deployment_run_id": manifest[0] if manifest else None,
        "blockers": blockers,
        "databricks": dbx,
    }
    _validation_evidence(db, project_id, "TEST_PRECHECK", "PASSED" if result["eligible"] else "BLOCKED",
                         environment="TEST", validation_type="TEST_PROMOTION_PRECHECK", details=result)
    return result


def _replace_catalog(value: str, source_catalog: str, target_catalog: str) -> str:
    value = value.replace(f"`{source_catalog}`", f"`{target_catalog}`")
    return re.sub(rf"(?i)(?<![A-Za-z0-9_]){re.escape(source_catalog)}(?![A-Za-z0-9_])", target_catalog, value)


def promote_medallion_to_test(db: Session, project_id: str) -> dict[str, Any]:
    """Promote the immutable, gate-approved DEV Medallion manifest into TEST."""
    precheck = test_promotion_precheck(db, project_id, test_databricks=True)
    if not precheck["eligible"]:
        raise ValueError("TEST promotion precheck blocked deployment: " + "; ".join(x["message"] for x in precheck["blockers"]))
    manifest = _latest_successful_medallion_run(db, project_id, "DEV")
    if not manifest:
        raise ValueError("No complete successful DEV Medallion deployment exists")

    cfg = get_settings()
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="TEST_DEPLOYMENT",
                       environment="TEST", status="RUNNING")
    db.add(run); db.commit()
    _deployment_evidence(db, project_id, run.id, "RUNNING", environment="TEST",
                         action="PROMOTE_DEV_TO_TEST", source_run_id=manifest[0])
    deployed: list[dict[str, Any]] = []
    failed_target = None
    try:
        for evidence, payload in sorted(
            manifest[1],
            key=lambda item: ({"BRONZE": 1, "SILVER": 2, "GOLD": 3}.get(str(item[1].get("layer")), 9),
                              str(item[1].get("target_fqn") or "").lower()),
        ):
            source_fqn = str(payload.get("target_fqn") or "")
            target_fqn = _replace_catalog(source_fqn, cfg.dev_catalog, cfg.test_catalog)
            failed_target = target_fqn
            catalog, schema, _ = _parse_target_fqn(target_fqn)
            _safe_create_catalog_and_schema(catalog, schema)
            owner = _target_owner_collision(db, project_id, target_fqn)
            if owner:
                raise RuntimeError(
                    f"Target ownership collision: {target_fqn} is already owned by project {owner.project_id} "
                    f"in workspace {databricks_workspace_identity()}"
                )
            node = db.get(MigrationMedallionNode, payload.get("medallion_node_id"))
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not node or not version:
                raise RuntimeError(f"DEV manifest metadata missing for {source_fqn}")
            if node.layer == "BRONZE" and node.node_type == "DELTA_TABLE":
                execute_sql(f"CREATE OR REPLACE TABLE {target_fqn} DEEP CLONE {source_fqn}", safe_retry=False)
                action = "DEEP_CLONE_DEV"
            else:
                execute_sql(_replace_catalog(version.content, cfg.dev_catalog, cfg.test_catalog), safe_retry=False)
                action = "EXECUTE_PROMOTED_ARTIFACT"
            _deployment_evidence(
                db, project_id, run.id, "PASSED", environment="TEST", object_id=evidence.object_id,
                target_fqn=target_fqn, source_target_fqn=source_fqn,
                medallion_node_id=node.id, layer=node.layer,
                artifact_version_id=version.id, artifact_version=version.version,
                promoted_from_environment="DEV", promoted_from_run_id=manifest[0], action=action,
            )
            deployed.append({"target_fqn": target_fqn, "layer": node.layer, "status": "PASSED",
                             "artifact_version_id": version.id})
        run.status = "PASSED"; run.ended_at = datetime.utcnow(); run.checkpoint = "TEST_DEPLOYMENT_COMPLETE"
        db.commit()
        _deployment_evidence(db, project_id, run.id, "PASSED", environment="TEST",
                             medallion_run_complete=True, promoted_from_run_id=manifest[0],
                             deployed=len(deployed), expected=len(manifest[1]))
        return {"run_id": run.id, "status": "PASSED", "source_run_id": manifest[0],
                "count": len(deployed), "deployed": deployed}
    except Exception as exc:
        run.status = "FAILED"; run.ended_at = datetime.utcnow(); run.checkpoint = failed_target
        db.commit()
        _deployment_evidence(db, project_id, run.id, "FAILED", environment="TEST",
                             failed_target=failed_target, error=str(exc), promoted_from_run_id=manifest[0])
        return {"run_id": run.id, "status": "FAILED", "failed_target": failed_target,
                "error": str(exc), "deployed": deployed}


def evaluate_test_gate(db: Session, project_id: str) -> dict[str, Any]:
    blockers: list[str] = []
    deployment = _latest_successful_medallion_run(db, project_id, "TEST")
    deployment_run_id = deployment[0] if deployment else None
    if not deployment:
        blockers.append("Latest TEST deployment is not PASSED")
    recon = db.scalars(select(MigrationReconciliation).where(
        MigrationReconciliation.project_id == project_id,
        MigrationReconciliation.environment == "TEST",
    ).order_by(MigrationReconciliation.created_at.desc())).first()
    if not recon or recon.status != "PASSED":
        blockers.append("Latest TEST reconciliation is not PASSED")
    open_issues = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_issues:
        blockers.append(f"{len(open_issues)} open blocker issue(s)")
    status = "PASSED" if not blockers else "BLOCKED"
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="QUALITY_GATE",
                       environment="TEST", status=status, ended_at=datetime.utcnow())
    db.add(run); db.flush()
    gate = MigrationQualityGate(id=uid("GAT"), project_id=project_id, run_id=run.id,
                                environment="TEST", status=status,
                                pass_count=2 if status == "PASSED" else max(0, 2 - len(blockers)),
                                fail_count=len(blockers), blocker_count=len(blockers),
                                deployment_version=deployment_run_id)
    db.add(gate); db.commit()
    return {"gate_id": gate.id, "run_id": run.id, "status": status,
            "blockers": blockers, "deployment_run_id": deployment_run_id}


def uat_promotion_precheck(db: Session, project_id: str, test_databricks: bool = True) -> dict[str, Any]:
    """Validate that the exact successful TEST Medallion manifest can enter UAT."""
    if not db.get(MigrationProject, project_id):
        raise ValueError("Project not found")
    blockers: list[dict[str, str]] = []
    test_gate = db.scalars(select(MigrationQualityGate).where(
        MigrationQualityGate.project_id == project_id,
        MigrationQualityGate.environment == "TEST",
    ).order_by(MigrationQualityGate.created_at.desc())).first()
    if not test_gate or test_gate.status != "PASSED":
        blockers.append({"code": "TEST_GATE", "message": "Latest TEST quality gate is not PASSED."})

    manifest = _latest_successful_medallion_run(db, project_id, "TEST")
    if not manifest:
        blockers.append({"code": "TEST_MANIFEST", "message": "No complete successful TEST Medallion deployment exists."})
    else:
        for _, payload in manifest[1]:
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not version or not version.executable or version.validation_status != "PASSED" or version.review_status != "APPROVED":
                blockers.append({
                    "code": "ARTIFACT_VERSION",
                    "message": f"{payload.get('target_fqn')} TEST artifact version is not approved, executable and validated.",
                })

    open_blockers = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_blockers:
        blockers.append({"code": "OPEN_ISSUES", "message": f"{len(open_blockers)} open blocker issue(s) must be resolved."})

    dbx = {"tested": False, "ok": None}
    if test_databricks:
        try:
            rows = execute_sql("SELECT current_catalog(), current_schema(), current_user()", safe_retry=True)
            dbx = {"tested": True, "ok": True, "result": [list(row) for row in rows]}
        except Exception as exc:
            blockers.append({"code": "DATABRICKS_CONNECTIVITY", "message": str(exc)})
            dbx = {"tested": True, "ok": False, "error": str(exc)}

    result = {
        "eligible": not blockers,
        "source_environment": "TEST",
        "target_environment": "UAT",
        "artifact_count": len(manifest[1]) if manifest else 0,
        "source_deployment_run_id": manifest[0] if manifest else None,
        "blockers": blockers,
        "databricks": dbx,
    }
    _validation_evidence(db, project_id, "UAT_PRECHECK", "PASSED" if result["eligible"] else "BLOCKED",
                         environment="UAT", validation_type="UAT_PROMOTION_PRECHECK", details=result)
    return result


def promote_medallion_to_uat(db: Session, project_id: str) -> dict[str, Any]:
    """Promote the immutable, gate-approved TEST Medallion manifest into UAT."""
    precheck = uat_promotion_precheck(db, project_id, test_databricks=True)
    if not precheck["eligible"]:
        raise ValueError("UAT promotion precheck blocked deployment: " + "; ".join(x["message"] for x in precheck["blockers"]))
    manifest = _latest_successful_medallion_run(db, project_id, "TEST")
    if not manifest:
        raise ValueError("No complete successful TEST Medallion deployment exists")

    cfg = get_settings()
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="UAT_DEPLOYMENT",
                       environment="UAT", status="RUNNING")
    db.add(run); db.commit()
    _deployment_evidence(db, project_id, run.id, "RUNNING", environment="UAT",
                         action="PROMOTE_TEST_TO_UAT", source_run_id=manifest[0])
    deployed: list[dict[str, Any]] = []
    failed_target = None
    try:
        for evidence, payload in sorted(
            manifest[1],
            key=lambda item: ({"BRONZE": 1, "SILVER": 2, "GOLD": 3}.get(str(item[1].get("layer")), 9),
                              str(item[1].get("target_fqn") or "").lower()),
        ):
            source_fqn = str(payload.get("target_fqn") or "")
            target_fqn = _replace_catalog(source_fqn, cfg.test_catalog, cfg.uat_catalog)
            failed_target = target_fqn
            catalog, schema, _ = _parse_target_fqn(target_fqn)
            _safe_create_catalog_and_schema(catalog, schema)
            owner = _target_owner_collision(db, project_id, target_fqn)
            if owner:
                raise RuntimeError(
                    f"Target ownership collision: {target_fqn} is already owned by project {owner.project_id} "
                    f"in workspace {databricks_workspace_identity()}"
                )
            node = db.get(MigrationMedallionNode, payload.get("medallion_node_id"))
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not node or not version:
                raise RuntimeError(f"TEST manifest metadata missing for {source_fqn}")
            if node.layer == "BRONZE" and node.node_type == "DELTA_TABLE":
                execute_sql(f"CREATE OR REPLACE TABLE {target_fqn} DEEP CLONE {source_fqn}", safe_retry=False)
                action = "DEEP_CLONE_TEST"
            else:
                promoted_sql = _replace_catalog(version.content, cfg.dev_catalog, cfg.uat_catalog)
                promoted_sql = _replace_catalog(promoted_sql, cfg.test_catalog, cfg.uat_catalog)
                execute_sql(promoted_sql, safe_retry=False)
                action = "EXECUTE_PROMOTED_ARTIFACT"
            _deployment_evidence(
                db, project_id, run.id, "PASSED", environment="UAT", object_id=evidence.object_id,
                target_fqn=target_fqn, source_target_fqn=source_fqn,
                medallion_node_id=node.id, layer=node.layer,
                artifact_version_id=version.id, artifact_version=version.version,
                promoted_from_environment="TEST", promoted_from_run_id=manifest[0], action=action,
            )
            deployed.append({"target_fqn": target_fqn, "layer": node.layer, "status": "PASSED",
                             "artifact_version_id": version.id})
        run.status = "PASSED"; run.ended_at = datetime.utcnow(); run.checkpoint = "UAT_DEPLOYMENT_COMPLETE"
        db.commit()
        _deployment_evidence(db, project_id, run.id, "PASSED", environment="UAT",
                             medallion_run_complete=True, promoted_from_run_id=manifest[0],
                             deployed=len(deployed), expected=len(manifest[1]))
        return {"run_id": run.id, "status": "PASSED", "source_run_id": manifest[0],
                "count": len(deployed), "deployed": deployed}
    except Exception as exc:
        run.status = "FAILED"; run.ended_at = datetime.utcnow(); run.checkpoint = failed_target
        db.commit()
        _deployment_evidence(db, project_id, run.id, "FAILED", environment="UAT",
                             failed_target=failed_target, error=str(exc), promoted_from_run_id=manifest[0])
        return {"run_id": run.id, "status": "FAILED", "failed_target": failed_target,
                "error": str(exc), "deployed": deployed}


def evaluate_uat_gate(db: Session, project_id: str) -> dict[str, Any]:
    blockers: list[str] = []
    deployment = _latest_successful_medallion_run(db, project_id, "UAT")
    deployment_run_id = deployment[0] if deployment else None
    if not deployment:
        blockers.append("Latest UAT deployment is not PASSED")
    recon = db.scalars(select(MigrationReconciliation).where(
        MigrationReconciliation.project_id == project_id,
        MigrationReconciliation.environment == "UAT",
    ).order_by(MigrationReconciliation.created_at.desc())).first()
    if not recon or recon.status != "PASSED":
        blockers.append("Latest UAT reconciliation is not PASSED")
    open_issues = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_issues:
        blockers.append(f"{len(open_issues)} open blocker issue(s)")
    status = "PASSED" if not blockers else "BLOCKED"
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="QUALITY_GATE",
                       environment="UAT", status=status, ended_at=datetime.utcnow())
    db.add(run); db.flush()
    gate = MigrationQualityGate(id=uid("GAT"), project_id=project_id, run_id=run.id,
                                environment="UAT", status=status,
                                pass_count=2 if status == "PASSED" else max(0, 2 - len(blockers)),
                                fail_count=len(blockers), blocker_count=len(blockers),
                                deployment_version=deployment_run_id)
    db.add(gate); db.commit()
    return {"gate_id": gate.id, "run_id": run.id, "status": status,
            "blockers": blockers, "deployment_run_id": deployment_run_id}


def prod_promotion_precheck(db: Session, project_id: str, test_databricks: bool = True) -> dict[str, Any]:
    """Validate that the exact successful UAT Medallion manifest can enter PROD."""
    if not db.get(MigrationProject, project_id):
        raise ValueError("Project not found")
    blockers: list[dict[str, str]] = []
    uat_gate = db.scalars(select(MigrationQualityGate).where(
        MigrationQualityGate.project_id == project_id,
        MigrationQualityGate.environment == "UAT",
    ).order_by(MigrationQualityGate.created_at.desc())).first()
    if not uat_gate or uat_gate.status != "PASSED":
        blockers.append({"code": "UAT_GATE", "message": "Latest UAT quality gate is not PASSED."})

    manifest = _latest_successful_medallion_run(db, project_id, "UAT")
    if not manifest:
        blockers.append({"code": "UAT_MANIFEST", "message": "No complete successful UAT Medallion deployment exists."})
    else:
        for _, payload in manifest[1]:
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not version or not version.executable or version.validation_status != "PASSED" or version.review_status != "APPROVED":
                blockers.append({
                    "code": "ARTIFACT_VERSION",
                    "message": f"{payload.get('target_fqn')} UAT artifact version is not approved, executable and validated.",
                })

    open_blockers = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_blockers:
        blockers.append({"code": "OPEN_ISSUES", "message": f"{len(open_blockers)} open blocker issue(s) must be resolved."})

    dbx = {"tested": False, "ok": None}
    if test_databricks:
        try:
            rows = execute_sql("SELECT current_catalog(), current_schema(), current_user()", safe_retry=True)
            dbx = {"tested": True, "ok": True, "result": [list(row) for row in rows]}
        except Exception as exc:
            blockers.append({"code": "DATABRICKS_CONNECTIVITY", "message": str(exc)})
            dbx = {"tested": True, "ok": False, "error": str(exc)}

    result = {
        "eligible": not blockers,
        "source_environment": "UAT",
        "target_environment": "PROD",
        "artifact_count": len(manifest[1]) if manifest else 0,
        "source_deployment_run_id": manifest[0] if manifest else None,
        "blockers": blockers,
        "databricks": dbx,
    }
    _validation_evidence(db, project_id, "PROD_PRECHECK", "PASSED" if result["eligible"] else "BLOCKED",
                         environment="PROD", validation_type="PROD_PROMOTION_PRECHECK", details=result)
    return result


def promote_medallion_to_prod(db: Session, project_id: str) -> dict[str, Any]:
    """Promote the immutable, gate-approved UAT Medallion manifest into PROD."""
    precheck = prod_promotion_precheck(db, project_id, test_databricks=True)
    if not precheck["eligible"]:
        raise ValueError("PROD promotion precheck blocked deployment: " + "; ".join(x["message"] for x in precheck["blockers"]))
    manifest = _latest_successful_medallion_run(db, project_id, "UAT")
    if not manifest:
        raise ValueError("No complete successful UAT Medallion deployment exists")

    cfg = get_settings()
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="PROD_DEPLOYMENT",
                       environment="PROD", status="RUNNING")
    db.add(run); db.commit()
    _deployment_evidence(db, project_id, run.id, "RUNNING", environment="PROD",
                         action="PROMOTE_UAT_TO_PROD", source_run_id=manifest[0])
    deployed: list[dict[str, Any]] = []
    failed_target = None
    try:
        for evidence, payload in sorted(
            manifest[1],
            key=lambda item: ({"BRONZE": 1, "SILVER": 2, "GOLD": 3}.get(str(item[1].get("layer")), 9),
                              str(item[1].get("target_fqn") or "").lower()),
        ):
            source_fqn = str(payload.get("target_fqn") or "")
            target_fqn = _replace_catalog(source_fqn, cfg.uat_catalog, cfg.prod_catalog)
            failed_target = target_fqn
            catalog, schema, _ = _parse_target_fqn(target_fqn)
            _safe_create_catalog_and_schema(catalog, schema)
            owner = _target_owner_collision(db, project_id, target_fqn)
            if owner:
                raise RuntimeError(
                    f"Target ownership collision: {target_fqn} is already owned by project {owner.project_id} "
                    f"in workspace {databricks_workspace_identity()}"
                )
            node = db.get(MigrationMedallionNode, payload.get("medallion_node_id"))
            version = db.get(MigrationStageArtifactVersion, payload.get("artifact_version_id"))
            if not node or not version:
                raise RuntimeError(f"UAT manifest metadata missing for {source_fqn}")
            if node.layer == "BRONZE" and node.node_type == "DELTA_TABLE":
                execute_sql(f"CREATE OR REPLACE TABLE {target_fqn} DEEP CLONE {source_fqn}", safe_retry=False)
                action = "DEEP_CLONE_UAT"
            else:
                promoted_sql = version.content
                for catalog_name in (cfg.dev_catalog, cfg.test_catalog, cfg.uat_catalog):
                    promoted_sql = _replace_catalog(promoted_sql, catalog_name, cfg.prod_catalog)
                execute_sql(promoted_sql, safe_retry=False)
                action = "EXECUTE_PROMOTED_ARTIFACT"
            _deployment_evidence(
                db, project_id, run.id, "PASSED", environment="PROD", object_id=evidence.object_id,
                target_fqn=target_fqn, source_target_fqn=source_fqn,
                medallion_node_id=node.id, layer=node.layer,
                artifact_version_id=version.id, artifact_version=version.version,
                promoted_from_environment="UAT", promoted_from_run_id=manifest[0], action=action,
            )
            deployed.append({"target_fqn": target_fqn, "layer": node.layer, "status": "PASSED",
                             "artifact_version_id": version.id})
        run.status = "PASSED"; run.ended_at = datetime.utcnow(); run.checkpoint = "PROD_DEPLOYMENT_COMPLETE"
        db.commit()
        _deployment_evidence(db, project_id, run.id, "PASSED", environment="PROD",
                             medallion_run_complete=True, promoted_from_run_id=manifest[0],
                             deployed=len(deployed), expected=len(manifest[1]))
        return {"run_id": run.id, "status": "PASSED", "source_run_id": manifest[0],
                "count": len(deployed), "deployed": deployed}
    except Exception as exc:
        run.status = "FAILED"; run.ended_at = datetime.utcnow(); run.checkpoint = failed_target
        db.commit()
        _deployment_evidence(db, project_id, run.id, "FAILED", environment="PROD",
                             failed_target=failed_target, error=str(exc), promoted_from_run_id=manifest[0])
        return {"run_id": run.id, "status": "FAILED", "failed_target": failed_target,
                "error": str(exc), "deployed": deployed}


def evaluate_prod_gate(db: Session, project_id: str) -> dict[str, Any]:
    blockers: list[str] = []
    deployment = _latest_successful_medallion_run(db, project_id, "PROD")
    deployment_run_id = deployment[0] if deployment else None
    if not deployment:
        blockers.append("Latest PROD deployment is not PASSED")
    recon = db.scalars(select(MigrationReconciliation).where(
        MigrationReconciliation.project_id == project_id,
        MigrationReconciliation.environment == "PROD",
    ).order_by(MigrationReconciliation.created_at.desc())).first()
    if not recon or recon.status != "PASSED":
        blockers.append("Latest PROD reconciliation is not PASSED")
    open_issues = db.scalars(select(MigrationIssue).where(
        MigrationIssue.project_id == project_id,
        MigrationIssue.status == "OPEN",
        MigrationIssue.severity == "BLOCKER",
    )).all()
    if open_issues:
        blockers.append(f"{len(open_issues)} open blocker issue(s)")
    status = "PASSED" if not blockers else "BLOCKED"
    run = MigrationRun(id=uid("RUN"), project_id=project_id, stage="QUALITY_GATE",
                       environment="PROD", status=status, ended_at=datetime.utcnow())
    db.add(run); db.flush()
    gate = MigrationQualityGate(id=uid("GAT"), project_id=project_id, run_id=run.id,
                                environment="PROD", status=status,
                                pass_count=2 if status == "PASSED" else max(0, 2 - len(blockers)),
                                fail_count=len(blockers), blocker_count=len(blockers),
                                deployment_version=deployment_run_id)
    db.add(gate); db.commit()
    return {"gate_id": gate.id, "run_id": run.id, "status": status,
            "blockers": blockers, "deployment_run_id": deployment_run_id}


def deployment_status(db: Session, project_id: str, environment: str = "DEV") -> dict[str, Any]:
    env = environment.upper()
    run = db.scalars(select(MigrationRun).where(
        MigrationRun.project_id == project_id,
        MigrationRun.environment == env,
        MigrationRun.stage == f"{env}_DEPLOYMENT",
    ).order_by(MigrationRun.started_at.desc())).first()
    if not run:
        return {"environment": env, "status": "NOT_STARTED", "run_id": None, "total": 0, "passed": 0, "failed": 0, "current_step": None, "failed_object": None, "logs": []}
    evidence = db.scalars(select(MigrationDeployment).where(
        MigrationDeployment.project_id == project_id, MigrationDeployment.environment == env,
    ).order_by(MigrationDeployment.created_at.asc())).all()
    relevant = [x for x in evidence if _payload(x).get("run_id") == run.id]
    object_rows = [x for x in relevant if x.object_id]
    logs = [{"status": x.status, "object_id": x.object_id, "created_at": x.created_at, **_payload(x)} for x in relevant[-50:]]
    fail = next((x for x in reversed(logs) if x.get("status") == "FAILED"), None)
    return {"environment": env, "status": run.status, "run_id": run.id, "started_at": run.started_at, "ended_at": run.ended_at,
            "checkpoint": run.checkpoint, "total": len(object_rows),
            "passed": sum(1 for x in object_rows if x.status == "PASSED"),
            "failed": sum(1 for x in object_rows if x.status == "FAILED"),
            "current_step": run.checkpoint, "failed_object": fail.get("failed_object") if fail else None, "logs": logs}
