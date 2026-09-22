from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Header, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.core.database import get_db
from app.core.security import hash_password, verify_password, create_access_token, decode_token
from app.models.entities import *
from app.models.canonical import MigrationDeployment, MigrationRunStep, MigrationValidation, MigrationReconciliation, MigrationReconciliationDetail
from app.services.engine import *
from app.services.discovery import (
    discover_sqlserver, test_sqlserver_connection,
    discover_postgres, test_postgres_connection,
    discover_mysql, test_mysql_connection,
    discover_source, test_source_connection,
)
from app.services.source_connector import connector_info, request as connector_request
from app.core.config import get_settings
from app.services.databricks_client import execute_sql
from app.services.type_compatibility import compatibility_catalog, transport_contract, transport_summary
from app.services.deployment import (
    dev_precheck, deploy_dev, latest_failed_dev_run, run_reconciliation,
    latest_reconciliation, evaluate_dev_gate, deployment_status,
    medallion_deployment_status,
    test_promotion_precheck, promote_medallion_to_test, evaluate_test_gate,
    uat_promotion_precheck, promote_medallion_to_uat, evaluate_uat_gate,
    prod_promotion_precheck, promote_medallion_to_prod, evaluate_prod_gate,
)
from app.services.medallion import (
    analyze_downstream_consumers, register_external_consumer, infer_semantics, infer_semantics_hybrid, list_semantics,
    upsert_explicit_semantic, approve_semantic, approve_all_semantics, build_medallion_plan, medallion_plan,
    generate_medallion_artifacts, list_medallion_artifacts, review_medallion_artifact,
    remediate_medallion_artifact, deploy_medallion_dev, approve_all_medallion_artifacts,
)
from app.services.ai_remediation import (
    accept_remediation,
    analyze_remediation,
    provider_status,
    test_provider_connection,
    list_provider_models,
    remediation_plan,
    remediate_one_artifact,
    run_remediation_batch,
)
import json
import csv
import io

router=APIRouter(prefix="/api")

class ProjectIn(BaseModel): name:str
class SourceIn(BaseModel): profile_name:str; server_name:str; database_name:str
class SnapshotIn(BaseModel): source_id:str; snapshot:dict
class MappingIn(BaseModel): environment:str="DEV"; catalog:str
class ArtifactIn(BaseModel): object_id:str; environment:str="DEV"
class LoginIn(BaseModel): username:str; password:str
class ReviewIn(BaseModel): artifact_version_id:str; review_type:str="ARCHITECT_REVIEW"; status:str; reviewer:str; comments:str|None=None
class GateIn(BaseModel): environment:str; status:str; pass_count:int=0; fail_count:int=0; blocker_count:int=0; deployment_version:str|None=None
class LayerOverrideIn(BaseModel): selected_layer:str; user:str; reason:str
class RemediationAnalyzeIn(BaseModel): environment:str="DEV"; use_ai:bool=True
class RemediationAcceptIn(BaseModel): ai_run_id:str; reviewer:str="admin"
class RemediationBatchIn(BaseModel):
    environment:str="DEV"
    use_ai:bool=True
    apply_valid_candidates:bool=True
    reviewer:str="admin"
    max_objects:int=100
class RemediationOneIn(BaseModel):
    environment:str="DEV"
    use_ai:bool=True
    reviewer:str="admin"
class IssueActionIn(BaseModel): action:str; comments:str

class ConsumerIn(BaseModel):
    object_id:str
    name:str
    consumer_type:str="BI_REPORT"
    usage_type:str="REPORTING_READ"
    evidence:dict={}

class SemanticIn(BaseModel):
    object_id:str
    semantic_role:str
    target_name:str|None=None
    grain:list[str]=[]
    business_keys:list[str]=[]
    dimension_keys:list[str]=[]
    attributes:list[str]=[]
    measures:list[dict]=[]
    scd_type:str|None=None
    notes:str|None=None

class SemanticApproveIn(BaseModel): actor:str="admin"; role:str|None=None
class MedallionPlanIn(BaseModel): environment:str="DEV"; catalog:str|None=None
class MedallionReviewIn(BaseModel): status:str="APPROVED"; reviewer:str="admin"
class MedallionDeployIn(BaseModel): allow_destructive:bool=False; batch_size:int=10000; max_rows:int|None=None

class DeployDevIn(BaseModel):
    allow_destructive: bool=False
    batch_size: int|None=None
    max_rows: int|None=None
    load_mode: str|None=None
    replace_existing_data: bool=False



def _decode_payload_json(value: str | None) -> dict:
    try: return json.loads(value or "{}")
    except Exception: return {}


def _environment_log_rows(db: Session, project_id: str, environment: str) -> list[dict]:
    env=environment.upper()
    rows=[]
    sources=[
        ("DEPLOYMENT", MigrationDeployment),
        ("RUN_STEP", MigrationRunStep),
        ("VALIDATION", MigrationValidation),
        ("RECONCILIATION", MigrationReconciliation),
        ("RECON_DETAIL", MigrationReconciliationDetail),
    ]
    for category, model in sources:
        q=select(model).where(model.project_id==project_id)
        if hasattr(model,"environment"):
            q=q.where((model.environment==env) | (model.environment==None))  # noqa: E711
        for row in db.scalars(q).all():
            payload=_decode_payload_json(getattr(row,"payload_json",None))
            rows.append({
                "timestamp": getattr(row,"created_at",None),
                "category": category,
                "status": getattr(row,"status",None),
                "environment": getattr(row,"environment",None),
                "object_id": getattr(row,"object_id",None),
                "run_id": payload.get("run_id"),
                "step": payload.get("step") or payload.get("action") or payload.get("validation_type"),
                "target_fqn": payload.get("target_fqn") or payload.get("target"),
                "message": payload.get("error") or payload.get("remediation") or payload.get("message"),
                "details": payload,
            })
    rows.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    return rows


def _dev_log_rows(db: Session, project_id: str) -> list[dict]:
    return _environment_log_rows(db,project_id,"DEV")


def _source_conn_for_source(src: MigrationSource) -> tuple[Any, str]:
    cfg = get_settings()
    st = (cfg.source_type or "ORACLE").upper()
    if st == "ORACLE" or (cfg.oracle_host and not cfg.mysql_host and not cfg.postgres_host and not cfg.sqlserver_host):
        host = cfg.oracle_host or src.server_name or "localhost"
        port = cfg.oracle_port or 1521
        db = src.database_name or cfg.oracle_service_name or cfg.oracle_sid or "ORCLPDB1"
        user = cfg.oracle_username or "system"
        pwd = cfg.oracle_password or ""
        return {
            "host": host, "port": port, "service_name": db,
            "schema": cfg.oracle_schema,
            "user": user, "password": pwd
        }, "ORACLE"
    if st == "MYSQL":
        host = cfg.mysql_host or src.server_name or "localhost"
        port = cfg.mysql_port or 3306
        db = src.database_name or cfg.mysql_database or ""
        user = cfg.mysql_username or "root"
        pwd = cfg.mysql_password or ""
        return {
            "host": host, "port": port, "database": db,
            "user": user, "password": pwd
        }, "MYSQL"
    if st in {"POSTGRESQL", "POSTGRES"} or (src.server_name and ":" in src.server_name) or (cfg.postgres_host and not cfg.sqlserver_host):
        host = cfg.postgres_host or src.server_name or "localhost"
        port = cfg.postgres_port or 5432
        db = src.database_name or cfg.postgres_database or "postgres"
        user = cfg.postgres_username or "postgres"
        pwd = cfg.postgres_password or ""
        sslmode = cfg.postgres_sslmode or "prefer"
        return {
            "host": host, "port": port, "dbname": db,
            "user": user, "password": pwd, "sslmode": sslmode
        }, "POSTGRESQL"
    driver = cfg.sqlserver_driver.replace("{", "").replace("}", "")
    if cfg.sqlserver_username:
        return f"DRIVER={{{driver}}};SERVER={src.server_name};DATABASE={src.database_name};UID={cfg.sqlserver_username};PWD={cfg.sqlserver_password or ''};TrustServerCertificate=yes;", "SQLSERVER"
    return f"DRIVER={{{driver}}};SERVER={src.server_name};DATABASE={src.database_name};Trusted_Connection=yes;TrustServerCertificate=yes;", "SQLSERVER"


def _sqlserver_conn_for_source(src: MigrationSource) -> str:
    conn, _ = _source_conn_for_source(src)
    return conn if isinstance(conn, str) else str(conn)


def auth(authorization: str|None=Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "): raise HTTPException(401,"Authentication required")
    try: return decode_token(authorization.split(" ",1)[1])
    except Exception: raise HTTPException(401,"Invalid or expired token")

@router.get("/health")
def health(): return {"status":"ok","service":"migration-factory"}

@router.post("/bootstrap-admin")
def bootstrap_admin(data:LoginIn,db:Session=Depends(get_db)):
    if db.scalar(select(func.count()).select_from(User))>0: raise HTTPException(409,"Users already exist")
    u=User(id=uid("USR"),username=data.username,password_hash=hash_password(data.password),role="ADMIN");db.add(u);db.commit();return {"created":True}

@router.post("/login")
def login(data:LoginIn,db:Session=Depends(get_db)):
    u=db.scalar(select(User).where(User.username==data.username))
    if not u or u.locked or not verify_password(data.password,u.password_hash if u else ""):
        if u:
            u.failed_attempts+=1
            if u.failed_attempts>=5:u.locked=True
            db.commit()
        raise HTTPException(401,"Invalid credentials or account locked")
    u.failed_attempts=0;db.commit();return {"access_token":create_access_token(u.username,u.role),"token_type":"bearer","role":u.role}

@router.post("/projects")
def projects_create(data:ProjectIn,db:Session=Depends(get_db),_=Depends(auth)):
    p=ensure_project(db,data.name); return {"id":p.id,"name":p.name,"status":p.status}

@router.get("/projects")
def projects_list(db:Session=Depends(get_db),_=Depends(auth)):
    return [{"id":p.id,"name":p.name,"status":p.status} for p in db.scalars(select(MigrationProject).order_by(MigrationProject.created_at.desc())).all()]


@router.get("/projects/{project_id}/sources")
def sources_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.scalars(select(MigrationSource).where(MigrationSource.project_id==project_id).order_by(MigrationSource.profile_name)).all()
    return [{"id":x.id,"profile_name":x.profile_name,"server_name":x.server_name,"database_name":x.database_name,
             "connector": connector_info(x.id)} for x in rows]

def _test_source(conn: Any, stype: str) -> dict[str, Any]:
    import app.api.routes as r
    import app.services.discovery as disc
    if getattr(r, "test_oracle_connection", None) and r.test_oracle_connection is not disc.test_oracle_connection:
        return r.test_oracle_connection(conn)
    if getattr(r, "test_mysql_connection", None) and r.test_mysql_connection is not disc.test_mysql_connection:
        return r.test_mysql_connection(conn)
    if getattr(r, "test_sqlserver_connection", None) and r.test_sqlserver_connection is not disc.test_sqlserver_connection:
        return r.test_sqlserver_connection(conn)
    if getattr(r, "test_postgres_connection", None) and r.test_postgres_connection is not disc.test_postgres_connection:
        return r.test_postgres_connection(conn)
    return test_source_connection(conn, stype)


@router.post("/projects/{project_id}/sources/{source_id}/test")
def source_test(project_id:str,source_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    src=db.get(MigrationSource,source_id)
    if not src or src.project_id!=project_id: raise HTTPException(404,"Source not found in project")
    try:
        conn, stype = _source_conn_for_source(src)
        result = (connector_request(src.id, "test") if connector_info(src.id)["mode"] == "CONNECTOR"
                  else _test_source(conn, stype))
        result.update({"profile_name":src.profile_name,"server_name":src.server_name,"database_name":src.database_name})
        return result
    except Exception as e:
        raise HTTPException(400,f"Connection test failed: {e}")

@router.post("/projects/{project_id}/discovery/live/{source_id}")
def discovery_live(project_id:str,source_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    src=db.get(MigrationSource,source_id)
    if not src or src.project_id!=project_id: raise HTTPException(404,"Source not found in project")
    conn, stype = _source_conn_for_source(src)
    try:
        if connector_info(src.id)["mode"] == "CONNECTOR":
            connector_request(src.id, "test")
            snap = connector_request(src.id, "discover")
        else:
            test_source_connection(conn, stype)
            snap = discover_source(conn, stype)
        return {"counts":ingest_snapshot(db,project_id,source_id,snap),"database":snap.get("database"),"objects":len(snap.get("objects",[]))}
    except Exception as e:
        raise HTTPException(400,f"Discovery failed: {e}")

@router.get("/projects/{project_id}/mappings")
def mappings_list(project_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.execute(select(MigrationMapping,MigrationObject).join(MigrationObject,MigrationObject.id==MigrationMapping.object_id).where(MigrationMapping.project_id==project_id,MigrationMapping.environment==environment.upper())).all()
    # Defensive de-duplication for databases created by older builds. One mapping per
    # source object/environment is effective; preserve legacy rows in the DB for audit.
    chosen={}
    for m,o in rows:
        prior=chosen.get(m.object_id)
        if prior is None or str(m.id) > str(prior[0].id):
            chosen[m.object_id]=(m,o)
    return [{"id":m.id,"object_id":m.object_id,"name":o.object_name,"type":o.object_type,"source_fqn":m.source_fqn,"target_fqn":m.target_fqn,"target_layer":m.target_layer,"environment":m.environment} for m,o in sorted(chosen.values(),key=lambda x:(x[1].schema_name.lower(),x[1].object_name.lower(),x[1].object_type))]

def _artifact_executable(content: str | None) -> bool:
    text=(content or "").upper()
    return bool(content) and "-- NON_EXECUTABLE:" not in text and "ARCHITECT_REVIEW_REQUIRED" not in text

def _latest_validation_for_version(db: Session, project_id: str, object_id: str, artifact_version_id: str | None, environment: str="DEV") -> tuple[str, dict]:
    if not artifact_version_id:
        return "NOT_RUN", {}
    rows=db.scalars(select(MigrationValidation).where(
        MigrationValidation.project_id==project_id,
        MigrationValidation.object_id==object_id,
        MigrationValidation.environment==environment.upper(),
    ).order_by(MigrationValidation.created_at.desc())).all()
    for row in rows:
        payload=_decode_payload_json(row.payload_json)
        if payload.get("artifact_version_id")==artifact_version_id:
            return row.status or "UNKNOWN", payload
    return "NOT_RUN", {}

def _latest_review_state(db: Session, project_id: str, artifact_version_id: str | None, review_type: str="ARCHITECT_REVIEW") -> str:
    if not artifact_version_id:
        return "PENDING"
    latest=db.scalars(select(MigrationReview).where(
        MigrationReview.project_id==project_id,
        MigrationReview.artifact_version_id==artifact_version_id,
        MigrationReview.review_type==review_type,
    ).order_by(MigrationReview.reviewed_at.desc())).first()
    return latest.status if latest else "PENDING"

@router.get("/projects/{project_id}/artifacts")
def artifacts_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.execute(select(MigrationArtifact,MigrationObject).join(MigrationObject,MigrationObject.id==MigrationArtifact.object_id).where(MigrationArtifact.project_id==project_id)).all()
    # Older builds could leave multiple MigrationArtifact rows for the same object.
    # Select exactly one effective current artifact per object without deleting audit history.
    chosen={}
    for a,o in rows:
        av=db.scalar(select(MigrationArtifactVersion).where(
            MigrationArtifactVersion.project_id==project_id,
            MigrationArtifactVersion.artifact_id==a.id,
            MigrationArtifactVersion.version==a.current_version,
        ))
        if not av:
            continue
        prior=chosen.get(a.object_id)
        if prior is None or (av.created_at,av.version,av.id) > (prior[2].created_at,prior[2].version,prior[2].id):
            chosen[a.object_id]=(a,o,av)
    out=[]
    for a,o,av in sorted(chosen.values(),key=lambda x:(x[1].schema_name.lower(),x[1].object_name.lower(),x[1].object_type)):
        content=av.content
        executable=_artifact_executable(content)
        validation_status,validation_payload=_latest_validation_for_version(db,project_id,o.id,av.id,"DEV")
        review_status=_latest_review_state(db,project_id,av.id)
        approval_blockers=[]
        if not executable: approval_blockers.append("Artifact is not executable")
        if validation_status!="PASSED": approval_blockers.append("Static validation must PASS for the current version")
        out.append({
            "artifact_id":a.id,"object_id":a.object_id,"schema":o.schema_name,"name":o.object_name,"type":a.artifact_type,
            "current_version":a.current_version,"artifact_version_id":av.id,"content":content,"executable":executable,
            "validation_status":validation_status,"validation_details":validation_payload,"review_status":review_status,
            "approval_allowed":not approval_blockers,"approval_blockers":approval_blockers,
            "ai_provider":av.ai_provider,"ai_model":av.ai_model
        })
    return out

@router.get("/projects/{project_id}/reviews")
def reviews_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.execute(
        select(MigrationReview,MigrationArtifactVersion,MigrationArtifact,MigrationObject)
        .join(MigrationArtifactVersion,MigrationArtifactVersion.id==MigrationReview.artifact_version_id)
        .join(MigrationArtifact,MigrationArtifact.id==MigrationArtifactVersion.artifact_id)
        .join(MigrationObject,MigrationObject.id==MigrationArtifact.object_id)
        .where(MigrationReview.project_id==project_id)
        .order_by(MigrationReview.reviewed_at.desc())
    ).all()
    out=[]; seen=set()
    for r,av,a,o in rows:
        # Collapse legacy duplicate clicks in the UI while preserving immutable audit rows in the database.
        key=(r.artifact_version_id,r.review_type,r.status,r.reviewer)
        if key in seen: continue
        seen.add(key)
        out.append({
            "id":r.id,"artifact_version_id":r.artifact_version_id,"review_type":r.review_type,"status":r.status,
            "reviewer":r.reviewer,"comments":r.comments,"reviewed_at":r.reviewed_at,
            "object_id":o.id,"schema":o.schema_name,"object_name":o.object_name,"object_type":o.object_type,
            "version":av.version,"artifact_type":a.artifact_type
        })
    return out

def _issue_details(db:Session, issue:MigrationIssue) -> dict:
    obj=db.get(MigrationObject,issue.object_id) if issue.object_id else None
    details=_decode_payload_json(issue.technical_details)
    if not details and issue.technical_details:
        details={"error":issue.technical_details}
    actions=[]
    for r in db.scalars(select(CanonicalRecord).where(
        CanonicalRecord.project_id==issue.project_id,
        CanonicalRecord.record_type=="ISSUE_ACTION",
        CanonicalRecord.object_id==issue.id
    ).order_by(CanonicalRecord.created_at.desc())).all():
        actions.append({"id":r.id,"created_at":r.created_at,**_decode_payload_json(r.payload_json)})
    return {
        "id":issue.id,"object_id":issue.object_id,"object_name":f"{obj.schema_name}.{obj.object_name}" if obj else None,
        "issue_type":issue.issue_type,"severity":issue.severity,"message":issue.message,
        "technical_details":details,"recommended_action":issue.recommended_action,"status":issue.status,
        "run_id":details.get("run_id"),"failed_object":details.get("failed_object") or (f"{obj.schema_name}.{obj.object_name}" if obj else None),
        "actions":actions
    }

@router.get("/projects/{project_id}/issues")
def issues_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.scalars(select(MigrationIssue).where(MigrationIssue.project_id==project_id)).all()
    return [_issue_details(db,i) for i in rows]

@router.get("/projects/{project_id}/issues/{issue_id}")
def issue_get(project_id:str,issue_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    issue=db.get(MigrationIssue,issue_id)
    if not issue or issue.project_id!=project_id: raise HTTPException(404,"Issue not found")
    return _issue_details(db,issue)

@router.post("/projects/{project_id}/issues/{issue_id}/action")
def issue_action(project_id:str,issue_id:str,data:IssueActionIn,db:Session=Depends(get_db),user=Depends(auth)):
    issue=db.get(MigrationIssue,issue_id)
    if not issue or issue.project_id!=project_id: raise HTTPException(404,"Issue not found")
    action=data.action.strip().upper(); comments=data.comments.strip()
    if action not in {"RESOLVE","CLOSE","REOPEN"}: raise HTTPException(400,"Action must be RESOLVE, CLOSE or REOPEN")
    if not comments: raise HTTPException(400,"Resolution comments are mandatory")
    target={"RESOLVE":"RESOLVED","CLOSE":"CLOSED","REOPEN":"OPEN"}[action]
    before=issue.status; issue.status=target
    db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="ISSUE_ACTION",object_id=issue.id,environment="DEV",payload_json=json.dumps({
        "action":action,"from_status":before,"to_status":target,"comments":comments,"actor":user.get("sub","unknown")
    })))
    db.commit(); return _issue_details(db,issue)

@router.post("/projects/{project_id}/issues/{issue_id}/recheck")
def issue_recheck(project_id:str,issue_id:str,db:Session=Depends(get_db),user=Depends(auth)):
    issue=db.get(MigrationIssue,issue_id)
    if not issue or issue.project_id!=project_id: raise HTTPException(404,"Issue not found")
    resolved=False; reason="No successful deployment evidence exists yet for this issue."
    if issue.issue_type=="DEPLOYMENT" and issue.object_id:
        for row in db.scalars(select(MigrationDeployment).where(MigrationDeployment.project_id==project_id).order_by(MigrationDeployment.created_at.desc())).all():
            payload=_decode_payload_json(getattr(row,"payload_json",None))
            if getattr(row,"status",None)=="PASSED" and getattr(row,"object_id",None)==issue.object_id:
                issue.status="RESOLVED"; resolved=True; reason="Automatically resolved from later successful deployment evidence."
                db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="ISSUE_ACTION",object_id=issue.id,environment="DEV",payload_json=json.dumps({
                    "action":"AUTO_RECHECK","from_status":"OPEN","to_status":"RESOLVED","comments":reason,"actor":user.get("sub","unknown")
                }))); db.commit(); break
    return {"resolved":resolved,"reason":reason,"issue":_issue_details(db,issue)}

@router.get("/projects/{project_id}/dependencies")
def dependencies_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.execute(select(MigrationDependency,MigrationObject).join(MigrationObject,MigrationObject.id==MigrationDependency.object_id).where(MigrationDependency.project_id==project_id)).all()
    return [{"id":d.id,"object_id":d.object_id,"object_name":o.object_name,"referenced_database":d.referenced_database,"referenced_schema":d.referenced_schema,"referenced_object":d.referenced_object,"referenced_column":d.referenced_column,"dependency_type":d.dependency_type} for d,o in rows]

@router.post("/projects/{project_id}/sources")
def source_create(project_id:str,data:SourceIn,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    s=add_source(db,project_id,data.profile_name,data.server_name,data.database_name);return {"id":s.id,"project_id":s.project_id}

@router.post("/projects/{project_id}/discovery/snapshot")
def discovery_snapshot(project_id:str,data:SnapshotIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:return {"counts":ingest_snapshot(db,project_id,data.source_id,data.snapshot)}
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/inventory")
def inventory(project_id:str,offset:int=0,limit:int=100,object_type:str|None=None,db:Session=Depends(get_db),_=Depends(auth)):
    q=select(MigrationObject).where(MigrationObject.project_id==project_id)
    if object_type:q=q.where(MigrationObject.object_type==object_type.upper())
    objs=db.scalars(q.order_by(MigrationObject.schema_name,MigrationObject.object_name).offset(offset).limit(min(limit,500))).all()
    return [{"id":o.id,"database":o.database_name,"schema":o.schema_name,"name":o.object_name,"type":o.object_type} for o in objs]

@router.post("/projects/{project_id}/classification")
def classify(project_id:str,db:Session=Depends(get_db),_=Depends(auth)): return {"classified":classify_project(db,project_id)}

@router.get("/projects/{project_id}/classification")
def classification_list(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.execute(select(MigrationClassification,MigrationObject).join(MigrationObject,MigrationObject.id==MigrationClassification.object_id).where(MigrationClassification.project_id==project_id)).all()
    return [{"object_id":c.object_id,"name":o.object_name,"type":o.object_type,"recommended_layer":c.recommended_layer,"selected_layer":c.selected_layer,"reason":c.classification_reason,"confidence":c.confidence_score} for c,o in rows]

@router.post("/projects/{project_id}/mappings")
def mappings(project_id:str,data:MappingIn,db:Session=Depends(get_db),_=Depends(auth)): return {"mapped":create_mappings(db,project_id,data.environment,data.catalog)}

@router.post("/projects/{project_id}/artifacts")
def artifact(project_id:str,data:ArtifactIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:a=generate_artifact(db,project_id,data.object_id,data.environment);return {"artifact_version_id":a.id,"version":a.version,"target_hash":a.target_hash,"content":a.content}
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/validate/{object_id}")
def validate(project_id:str,object_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)): return static_validate(db,project_id,object_id,environment)

@router.post("/projects/{project_id}/reviews")
def review(project_id:str,data:ReviewIn,db:Session=Depends(get_db),_=Depends(auth)):
    av=db.get(MigrationArtifactVersion,data.artifact_version_id)
    if not av or av.project_id!=project_id: raise HTTPException(404,"Artifact version not found in project")
    status=data.status.upper().strip()
    allowed={"APPROVED","REJECTED","REVOKED","CHANGES_REQUESTED"}
    if status not in allowed:
        raise HTTPException(400,f"Review status must be one of: {', '.join(sorted(allowed))}")
    art=db.get(MigrationArtifact,av.artifact_id)
    obj=db.get(MigrationObject,art.object_id) if art else None
    if not art or not obj or art.project_id!=project_id or obj.project_id!=project_id:
        raise HTTPException(404,"Artifact/object not found in project")
    if av.version!=art.current_version:
        raise HTTPException(400,"Only the current artifact version can receive a new review decision")

    if status=="APPROVED":
        blockers=[]
        if not _artifact_executable(av.content): blockers.append("artifact is not executable")
        validation_status,_=_latest_validation_for_version(db,project_id,obj.id,av.id,"DEV")
        if validation_status!="PASSED": blockers.append("static validation for the current artifact version has not PASSED")
        if blockers:
            raise HTTPException(400,"Approval blocked: "+"; ".join(blockers)+". Run remediation/static validation first.")
    else:
        if not (data.comments or "").strip():
            raise HTTPException(400,f"A reason/comment is required for {status}")
        if status=="REVOKED":
            current=_latest_review_state(db,project_id,av.id,data.review_type)
            if current!="APPROVED":
                raise HTTPException(400,"Only an effectively APPROVED artifact can have its approval revoked")

    # Exact repeated actions are idempotent, while state changes remain immutable audit events.
    latest=db.scalars(select(MigrationReview).where(
        MigrationReview.project_id==project_id,
        MigrationReview.artifact_version_id==av.id,
        MigrationReview.review_type==data.review_type,
    ).order_by(MigrationReview.reviewed_at.desc())).first()
    if latest and latest.status==status and latest.reviewer==data.reviewer and (latest.comments or "")== (data.comments or ""):
        return {"id":latest.id,"status":latest.status,"effective_status":latest.status,"duplicate_prevented":True}
    r=MigrationReview(id=uid("REV"),project_id=project_id,artifact_version_id=av.id,review_type=data.review_type,status=status,reviewer=data.reviewer,comments=data.comments)
    db.add(r);db.commit()
    return {"id":r.id,"status":r.status,"effective_status":r.status,"duplicate_prevented":False}

@router.post("/projects/{project_id}/artifacts/{object_id}/remediation/analyze")
def remediation_analyze(project_id:str,object_id:str,data:RemediationAnalyzeIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        return analyze_remediation(db,project_id,object_id,data.environment,data.use_ai)
    except (ValueError,RuntimeError) as e:
        raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/artifacts/{object_id}/remediation/accept")
def remediation_accept(project_id:str,object_id:str,data:RemediationAcceptIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        av=accept_remediation(db,project_id,object_id,data.ai_run_id,data.reviewer)
        return {"accepted":True,"artifact_version_id":av.id,"version":av.version,"approval_required":True,"auto_deployed":False}
    except ValueError as e:
        raise HTTPException(400,str(e))

@router.get("/ai/provider-status")
def ai_provider_status(probe:bool=False,_:dict=Depends(auth)):
    return test_provider_connection() if probe else provider_status()

@router.post("/ai/provider-test")
def ai_provider_test(_:dict=Depends(auth)):
    return test_provider_connection()

@router.get("/ai/models")
def ai_models(_:dict=Depends(auth)):
    return list_provider_models()


@router.get("/compatibility/catalog")
def compatibility_catalog_api(_:dict=Depends(auth)):
    return {"framework_version":"2.2.0","deterministic_first":True,"adapters":compatibility_catalog()}

@router.get("/projects/{project_id}/compatibility/summary")
def compatibility_summary_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id):
        raise HTTPException(404,"Project not found")
    objects=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id).order_by(MigrationObject.schema_name,MigrationObject.object_name)).all()
    object_rows=[]
    all_contract=[]
    for obj in objects:
        if obj.object_type not in {"TABLE","VIEW"}:
            continue
        cols=db.scalars(select(MigrationColumn).where(
            MigrationColumn.project_id==project_id,
            MigrationColumn.object_id==obj.id,
        ).order_by(MigrationColumn.ordinal)).all()
        if not cols:
            continue
        contract=transport_contract(cols)
        summary=transport_summary(cols)
        all_contract.extend(contract)
        object_rows.append({
            "object_id":obj.id,
            "name":f"{obj.schema_name}.{obj.object_name}",
            "type":obj.object_type,
            "summary":summary,
            "columns":contract,
        })
    strategy_counts={}
    family_counts={}
    review=[]
    unknown=[]
    for item in all_contract:
        strategy_counts[item["strategy"]]=strategy_counts.get(item["strategy"],0)+1
        family_counts[item["family"]]=family_counts.get(item["family"],0)+1
        if item["review_required"]:
            review.append(item)
        if item["family"]=="UNKNOWN":
            unknown.append(item)
    deterministic=sum(1 for item in all_contract if item["deterministic"] and not item["review_required"])
    total=len(all_contract)
    return {
        "framework_version":"2.2.0",
        "project_id":project_id,
        "total_columns":total,
        "deterministic_columns":deterministic,
        "deterministic_coverage_pct":round((deterministic/total*100.0),2) if total else 100.0,
        "review_required_count":len(review),
        "unknown_type_count":len(unknown),
        "strategy_counts":strategy_counts,
        "family_counts":family_counts,
        "objects":object_rows,
        "policy":{
            "unknown_types":"GOVERNED_TEXT_FALLBACK_REVIEW_REQUIRED",
            "ai_for_runtime_transport":False,
            "ai_for_semantic_conversion":True,
            "production_auto_mutation":False,
        },
    }

@router.get("/projects/{project_id}/remediation/plan")
def remediation_plan_api(project_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    try:
        return remediation_plan(db,project_id,environment)
    except ValueError as e:
        raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/remediation/run")
def remediation_run_api(project_id:str,data:RemediationBatchIn,db:Session=Depends(get_db),user=Depends(auth)):
    try:
        reviewer=data.reviewer.strip() or user.get("sub","unknown")
        return run_remediation_batch(
            db,project_id,environment=data.environment,use_ai=data.use_ai,
            apply_valid_candidates=data.apply_valid_candidates,reviewer=reviewer,max_objects=data.max_objects,
        )
    except (ValueError,RuntimeError) as e:
        raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/artifacts/{object_id}/remediation/repair")
def remediation_repair_one_api(project_id:str,object_id:str,data:RemediationOneIn,db:Session=Depends(get_db),user=Depends(auth)):
    try:
        reviewer=data.reviewer.strip() or user.get("sub","unknown")
        return remediate_one_artifact(
            db,project_id,object_id,environment=data.environment,use_ai=data.use_ai,reviewer=reviewer,
        )
    except (ValueError,RuntimeError) as e:
        raise HTTPException(400,str(e))


@router.post("/projects/{project_id}/consumers/analyze")
def consumers_analyze_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return analyze_downstream_consumers(db,project_id)
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/consumers")
def consumers_list_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    rows=db.scalars(select(MigrationConsumer).where(MigrationConsumer.project_id==project_id).order_by(MigrationConsumer.producer_object_id,MigrationConsumer.dependency_depth,MigrationConsumer.consumer_name)).all()
    return [{"id":x.id,"producer_object_id":x.producer_object_id,"consumer_object_id":x.consumer_object_id,
             "consumer_name":x.consumer_name,"consumer_type":x.consumer_type,"usage_type":x.usage_type,
             "dependency_depth":x.dependency_depth,"evidence_type":x.evidence_type,
             "evidence":_decode_payload_json(x.evidence_json),"confidence":x.confidence_score} for x in rows]

@router.post("/projects/{project_id}/consumers")
def consumers_register_api(project_id:str,data:ConsumerIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        row=register_external_consumer(db,project_id,data.object_id,name=data.name,consumer_type=data.consumer_type,
                                       usage_type=data.usage_type,evidence=data.evidence)
        return {"id":row.id,"registered":True}
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/semantics/infer")
def semantics_infer_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return infer_semantics_hybrid(db,project_id)
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/semantics")
def semantics_list_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return list_semantics(db,project_id)

@router.post("/projects/{project_id}/semantics")
def semantics_upsert_api(project_id:str,data:SemanticIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        row=upsert_explicit_semantic(db,project_id,data.object_id,data.model_dump())
        return {"id":row.id,"status":row.status,"semantic_role":row.semantic_role,"target_name":row.target_name}
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/semantics/{semantic_id}/approve")
def semantics_approve_api(project_id:str,semantic_id:str,data:SemanticApproveIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        row=approve_semantic(db,project_id,semantic_id,data.actor,role=data.role)
        return {"id":row.id,"status":row.status,"approved_by":row.approved_by,"approved_at":row.approved_at,"semantic_role":row.semantic_role,"target_name":row.target_name}
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/semantics/approve-all")
def semantics_approve_all_api(project_id:str,data:SemanticApproveIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        return approve_all_semantics(db,project_id,data.actor)
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/medallion/plan")
def medallion_build_api(project_id:str,data:MedallionPlanIn,db:Session=Depends(get_db),_=Depends(auth)):
    try: return build_medallion_plan(db,project_id,environment=data.environment,catalog=data.catalog)
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/medallion/plan")
def medallion_get_api(project_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    return medallion_plan(db,project_id,environment=environment)

@router.post("/projects/{project_id}/medallion/generate")
def medallion_generate_api(project_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    try: return generate_medallion_artifacts(db,project_id,environment=environment)
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/medallion/artifacts")
def medallion_artifacts_api(project_id:str,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    return list_medallion_artifacts(db,project_id,environment=environment)

@router.post("/projects/{project_id}/medallion/artifacts/approve-all")
def medallion_artifacts_approve_all_api(project_id:str,data:MedallionReviewIn,environment:str="DEV",db:Session=Depends(get_db),_=Depends(auth)):
    try:
        return approve_all_medallion_artifacts(db,project_id,environment=environment,reviewer=data.reviewer)
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/medallion/artifacts/{version_id}/review")
def medallion_artifact_review_api(project_id:str,version_id:str,data:MedallionReviewIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        row=review_medallion_artifact(db,project_id,version_id,status=data.status,reviewer=data.reviewer)
        return {"artifact_version_id":row.id,"review_status":row.review_status,"reviewer":row.reviewer,"reviewed_at":row.reviewed_at}
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/medallion/artifacts/{version_id}/remediate")
def medallion_artifact_remediate_api(project_id:str,version_id:str,data:RemediationOneIn,db:Session=Depends(get_db),user=Depends(auth)):
    try:
        return remediate_medallion_artifact(
            db,project_id,version_id,environment=data.environment,use_ai=data.use_ai,
            reviewer=getattr(user,"username",None) or data.reviewer,
        )
    except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/medallion/deploy-dev")
def medallion_deploy_api(project_id:str,data:MedallionDeployIn,db:Session=Depends(get_db),_=Depends(auth)):
    try: return deploy_medallion_dev(db,project_id,allow_destructive=data.allow_destructive,batch_size=data.batch_size,max_rows=data.max_rows)
    except (ValueError,RuntimeError) as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/quality-gates")
def quality_gate(project_id:str,data:GateIn,db:Session=Depends(get_db),_=Depends(auth)):
    run=MigrationRun(id=uid("RUN"),project_id=project_id,stage="QUALITY_GATE",environment=data.environment.upper(),status=data.status,ended_at=datetime.utcnow());db.add(run);db.flush()
    g=MigrationQualityGate(id=uid("GAT"),project_id=project_id,run_id=run.id,environment=data.environment.upper(),status=data.status,pass_count=data.pass_count,fail_count=data.fail_count,blocker_count=data.blocker_count,deployment_version=data.deployment_version);db.add(g);db.commit();return {"run_id":run.id,"gate_id":g.id}

@router.get("/projects/{project_id}/lifecycle")
def lifecycle_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)): return lifecycle(db,project_id)

@router.get("/projects/{project_id}/dashboard")
def dashboard(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    objs=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id)).all(); cls=db.scalars(select(MigrationClassification).where(MigrationClassification.project_id==project_id)).all(); issues=db.scalars(select(MigrationIssue).where(MigrationIssue.project_id==project_id,MigrationIssue.status=="OPEN")).all()
    bytype=defaultdict(int);bylayer=defaultdict(int)
    for o in objs:bytype[o.object_type]+=1
    for c in cls:bylayer[c.selected_layer]+=1
    nodes=db.scalars(select(MigrationMedallionNode).where(MigrationMedallionNode.project_id==project_id,MigrationMedallionNode.environment=="DEV")).all()
    stage_layers=defaultdict(int)
    for n in nodes: stage_layers[n.layer]+=1
    sem=db.scalars(select(MigrationSemanticDefinition).where(MigrationSemanticDefinition.project_id==project_id)).all()
    consumers=db.scalars(select(MigrationConsumer).where(MigrationConsumer.project_id==project_id)).all()
    return {"objects_discovered":len(objs),"types":dict(bytype),"layers":dict(bylayer),"medallion_layers":dict(stage_layers),
            "semantic_facts":sum(1 for x in sem if x.semantic_role=="FACT"),"semantic_dimensions":sum(1 for x in sem if x.semantic_role=="DIMENSION"),
            "approved_gold_semantics":sum(1 for x in sem if x.status=="APPROVED" and x.semantic_role in {"FACT","DIMENSION","AGGREGATE","KPI","REPORTING"}),
            "downstream_consumers":len(consumers),"blocked_objects":sum(1 for i in issues if i.severity=="BLOCKER"),
            "review_required":sum(1 for o in objs if o.object_type in {"TRIGGER"}) + sum(1 for x in sem if x.status=="REVIEW_REQUIRED")}

@router.put("/projects/{project_id}/classification/{object_id}")
def layer_override(project_id:str,object_id:str,data:LayerOverrideIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        c=override_layer(db,project_id,object_id,data.selected_layer,data.user,data.reason);return {"object_id":c.object_id,"selected_layer":c.selected_layer,"override_user":c.override_user,"override_reason":c.override_reason}
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/promotion-precheck/{environment}")
def precheck(project_id:str,environment:str,db:Session=Depends(get_db),_=Depends(auth)):
    return promotion_precheck(db,project_id,environment)


@router.post("/projects/{project_id}/deployments/dev/test-databricks")
def deployment_test_databricks(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    try:
        rows=execute_sql("SELECT current_catalog(), current_schema(), current_user()",safe_retry=True)
        return {"ok":True,"environment":"DEV","result":[list(r) for r in rows]}
    except Exception as e:
        raise HTTPException(400,f"Databricks connection test failed: {e}")

@router.post("/projects/{project_id}/deployments/dev/precheck")
def deployment_precheck(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return dev_precheck(db,project_id,test_databricks=True)
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/deployments/dev/deploy")
def deployment_dev(project_id:str,data:DeployDevIn,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        result=deploy_dev(db,project_id,allow_destructive=data.allow_destructive,batch_size=data.batch_size,max_rows=data.max_rows,load_mode=data.load_mode,replace_existing_data=data.replace_existing_data)
        if result.get("status")=="FAILED": raise HTTPException(400,result)
        return result
    except ValueError as e: raise HTTPException(400,str(e))

@router.post("/projects/{project_id}/deployments/dev/resume")
def deployment_resume(project_id:str,data:DeployDevIn,db:Session=Depends(get_db),_=Depends(auth)):
    failed=latest_failed_dev_run(db,project_id)
    if not failed: raise HTTPException(404,"No failed DEV deployment run is available to resume")
    result=deploy_dev(db,project_id,allow_destructive=data.allow_destructive,batch_size=data.batch_size,max_rows=data.max_rows,load_mode=data.load_mode,replace_existing_data=data.replace_existing_data,resume_run_id=failed.id)
    if result.get("status")=="FAILED": raise HTTPException(400,result)
    return result

@router.post("/projects/{project_id}/deployments/dev/reconcile")
def deployment_reconcile(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return run_reconciliation(db,project_id,"DEV")
    except ValueError as e: raise HTTPException(400,str(e))

@router.get("/projects/{project_id}/deployments/dev/reconciliation/latest")
def deployment_reconciliation_latest(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    return latest_reconciliation(db,project_id,"DEV") or {
        "status":"NOT_STARTED","workflow":"MEDALLION","run_id":None,
        "passed":0,"failed":0,"details_count":0,"details":[],
    }

@router.get("/projects/{project_id}/deployments/dev/reconciliation/latest/download")
def deployment_reconciliation_download(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    result=latest_reconciliation(db,project_id,"DEV")
    if not result: raise HTTPException(404,"No DEV reconciliation result exists")
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["run_id","layer","object","object_type","target_fqn","check","source_count","target_count","status","artifact_version","artifact_version_id","error"])
    for row in result.get("details",[]):
        writer.writerow([
            result.get("run_id"),row.get("layer"),row.get("object"),row.get("object_type"),
            row.get("target_fqn"),row.get("reconciliation_type"),row.get("source_count"),
            row.get("target_count"),row.get("status"),row.get("artifact_version"),
            row.get("artifact_version_id"),row.get("error"),
        ])
    return Response(
        content=out.getvalue(),media_type="text/csv",
        headers={"Content-Disposition":f'attachment; filename="medallion_reconciliation_{result.get("run_id")}.csv"'},
    )

@router.post("/projects/{project_id}/deployments/dev/evaluate-gate")
def deployment_gate(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return evaluate_dev_gate(db,project_id)

@router.get("/projects/{project_id}/deployments/dev/status")
def deployment_status_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return deployment_status(db,project_id,"DEV")

@router.get("/projects/{project_id}/medallion/deployments/dev/status")
def medallion_deployment_status_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return medallion_deployment_status(db,project_id,"DEV")


@router.post("/projects/{project_id}/promotions/test/precheck")
def test_promotion_precheck_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return test_promotion_precheck(db,project_id,test_databricks=True)
    except ValueError as e: raise HTTPException(400,str(e))


@router.post("/projects/{project_id}/promotions/test/deploy")
def test_promotion_deploy_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        result=promote_medallion_to_test(db,project_id)
        if result.get("status")=="FAILED":
            err = result.get("error") or "TEST promotion failed"
            if result.get("failed_target"):
                err = f"[{result['failed_target']}] {err}"
            raise HTTPException(400, err)
        return result
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/test/status")
def test_promotion_status_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return deployment_status(db,project_id,"TEST")


@router.post("/projects/{project_id}/promotions/test/reconcile")
def test_promotion_reconcile_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return run_reconciliation(db,project_id,"TEST")
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/test/reconciliation/latest")
def test_reconciliation_latest_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    return latest_reconciliation(db,project_id,"TEST") or {
        "status":"NOT_STARTED","workflow":"MEDALLION","run_id":None,
        "passed":0,"failed":0,"details_count":0,"details":[],
    }


@router.post("/projects/{project_id}/promotions/test/evaluate-gate")
def test_gate_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return evaluate_test_gate(db,project_id)


@router.get("/projects/{project_id}/promotions/test/logs")
def test_promotion_logs_api(project_id:str,limit:int=1000,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_environment_log_rows(db,project_id,"TEST")[:max(1,min(limit,5000))]
    return {"project_id":project_id,"environment":"TEST","count":len(rows),"logs":rows}


@router.post("/projects/{project_id}/promotions/uat/precheck")
def uat_promotion_precheck_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return uat_promotion_precheck(db,project_id,test_databricks=True)
    except ValueError as e: raise HTTPException(400,str(e))


@router.post("/projects/{project_id}/promotions/uat/deploy")
def uat_promotion_deploy_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        result=promote_medallion_to_uat(db,project_id)
        if result.get("status")=="FAILED":
            err = result.get("error") or "UAT promotion failed"
            if result.get("failed_target"):
                err = f"[{result['failed_target']}] {err}"
            raise HTTPException(400, err)
        return result
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/uat/status")
def uat_promotion_status_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return deployment_status(db,project_id,"UAT")


@router.post("/projects/{project_id}/promotions/uat/reconcile")
def uat_promotion_reconcile_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return run_reconciliation(db,project_id,"UAT")
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/uat/reconciliation/latest")
def uat_reconciliation_latest_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    return latest_reconciliation(db,project_id,"UAT") or {
        "status":"NOT_STARTED","workflow":"MEDALLION","run_id":None,
        "passed":0,"failed":0,"details_count":0,"details":[],
    }


@router.post("/projects/{project_id}/promotions/uat/evaluate-gate")
def uat_gate_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return evaluate_uat_gate(db,project_id)


@router.get("/projects/{project_id}/promotions/uat/logs")
def uat_promotion_logs_api(project_id:str,limit:int=1000,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_environment_log_rows(db,project_id,"UAT")[:max(1,min(limit,5000))]
    return {"project_id":project_id,"environment":"UAT","count":len(rows),"logs":rows}


@router.post("/projects/{project_id}/promotions/prod/precheck")
def prod_promotion_precheck_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return prod_promotion_precheck(db,project_id,test_databricks=True)
    except ValueError as e: raise HTTPException(400,str(e))


@router.post("/projects/{project_id}/promotions/prod/deploy")
def prod_promotion_deploy_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try:
        result=promote_medallion_to_prod(db,project_id)
        if result.get("status")=="FAILED":
            err = result.get("error") or "PROD promotion failed"
            if result.get("failed_target"):
                err = f"[{result['failed_target']}] {err}"
            raise HTTPException(400, err)
        return result
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/prod/status")
def prod_promotion_status_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return deployment_status(db,project_id,"PROD")


@router.post("/projects/{project_id}/promotions/prod/reconcile")
def prod_promotion_reconcile_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    try: return run_reconciliation(db,project_id,"PROD")
    except ValueError as e: raise HTTPException(400,str(e))


@router.get("/projects/{project_id}/promotions/prod/reconciliation/latest")
def prod_reconciliation_latest_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    return latest_reconciliation(db,project_id,"PROD") or {
        "status":"NOT_STARTED","workflow":"MEDALLION","run_id":None,
        "passed":0,"failed":0,"details_count":0,"details":[],
    }


@router.post("/projects/{project_id}/promotions/prod/evaluate-gate")
def prod_gate_api(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    return evaluate_prod_gate(db,project_id)


@router.get("/projects/{project_id}/promotions/prod/logs")
def prod_promotion_logs_api(project_id:str,limit:int=1000,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_environment_log_rows(db,project_id,"PROD")[:max(1,min(limit,5000))]
    return {"project_id":project_id,"environment":"PROD","count":len(rows),"logs":rows}


@router.get("/projects/{project_id}/deployments/dev/logs")
def deployment_logs(project_id:str,limit:int=500,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_dev_log_rows(db,project_id)[:max(1,min(limit,5000))]
    return {"project_id":project_id,"environment":"DEV","count":len(rows),"logs":rows}


@router.get("/projects/{project_id}/deployments/dev/logs/download")
def deployment_logs_download(project_id:str,format:str="csv",db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_dev_log_rows(db,project_id)
    stamp=datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if format.lower()=="json":
        body=json.dumps({"project_id":project_id,"environment":"DEV","generated_at":datetime.utcnow().isoformat()+"Z","logs":rows},default=str,indent=2)
        return Response(content=body,media_type="application/json",headers={"Content-Disposition":f'attachment; filename="migration_dev_logs_{stamp}.json"'})
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["timestamp","category","status","environment","run_id","object_id","step","target_fqn","message","details_json"])
    for r in rows:
        writer.writerow([r.get("timestamp"),r.get("category"),r.get("status"),r.get("environment"),r.get("run_id"),r.get("object_id"),r.get("step"),r.get("target_fqn"),r.get("message"),json.dumps(r.get("details") or {},default=str,sort_keys=True)])
    return Response(content=out.getvalue(),media_type="text/csv",headers={"Content-Disposition":f'attachment; filename="migration_dev_logs_{stamp}.csv"'})


def _medallion_run_log_rows(db:Session,project_id:str,run_id:str) -> list[dict]:
    return [row for row in _dev_log_rows(db,project_id)
            if row.get("run_id")==run_id and (row.get("details") or {}).get("medallion_node_id")]


@router.get("/projects/{project_id}/medallion/deployments/{run_id}/logs")
def medallion_deployment_logs(project_id:str,run_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_medallion_run_log_rows(db,project_id,run_id)
    return {
        "project_id":project_id,"environment":"DEV","run_id":run_id,"count":len(rows),
        "passed":sum(1 for row in rows if row.get("status")=="PASSED"),
        "failed":sum(1 for row in rows if row.get("status")=="FAILED"),"logs":rows,
    }


@router.get("/projects/{project_id}/medallion/deployments/{run_id}/logs/download")
def medallion_deployment_logs_download(project_id:str,run_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rows=_medallion_run_log_rows(db,project_id,run_id)
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["timestamp","run_id","status","layer","target_fqn","artifact_version_id","object_id","error","details_json"])
    for row in rows:
        details=row.get("details") or {}
        writer.writerow([
            row.get("timestamp"),run_id,row.get("status"),details.get("layer"),row.get("target_fqn"),
            details.get("artifact_version_id"),row.get("object_id"),details.get("error") or row.get("message"),
            json.dumps(details,default=str,sort_keys=True),
        ])
    return Response(
        content=out.getvalue(),media_type="text/csv",
        headers={"Content-Disposition":f'attachment; filename="medallion_{run_id}_logs.csv"'},
    )


class RecordIn(BaseModel):
    title:str
    status:str="OPEN"
    environment:str|None=None
    object_id:str|None=None
    details:dict={}

MODULE_TYPES={
    "assessment":"ASSESSMENT","conversion-plans":"CONVERSION_PLAN","data-quality":"DATA_QUALITY",
    "reconciliation":"RECONCILIATION","deployments":"DEPLOYMENT","waves":"WAVE",
    "cutover":"CUTOVER","decommission":"DECOMMISSION","governance":"GOVERNANCE",
    "audit":"AUDIT","administration":"ADMINISTRATION"
}

def _record_to_dict(r:CanonicalRecord):
    try: payload=json.loads(r.payload_json or "{}")
    except Exception: payload={}
    return {"id":r.id,"record_type":r.record_type,"object_id":r.object_id,"environment":r.environment,"created_at":r.created_at,"payload":payload}

@router.get("/projects/{project_id}/module/{module_name}")
def module_records(project_id:str,module_name:str,db:Session=Depends(get_db),_=Depends(auth)):
    rt=MODULE_TYPES.get(module_name)
    if not rt: raise HTTPException(404,"Unknown module")
    rows=db.scalars(select(CanonicalRecord).where(CanonicalRecord.project_id==project_id,CanonicalRecord.record_type==rt).order_by(CanonicalRecord.created_at.desc())).all()
    return [_record_to_dict(r) for r in rows]

@router.post("/projects/{project_id}/module/{module_name}")
def module_create(project_id:str,module_name:str,data:RecordIn,db:Session=Depends(get_db),_=Depends(auth)):
    if not db.get(MigrationProject,project_id): raise HTTPException(404,"Project not found")
    rt=MODULE_TYPES.get(module_name)
    if not rt: raise HTTPException(404,"Unknown module")
    payload={"title":data.title,"status":data.status,"details":data.details}
    r=CanonicalRecord(id=uid("REC"),project_id=project_id,record_type=rt,object_id=data.object_id,environment=(data.environment.upper() if data.environment else None),payload_json=json.dumps(payload,default=str))
    db.add(r);db.commit();return _record_to_dict(r)

@router.post("/projects/{project_id}/assessment/run")
def assessment_run(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    objs=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id)).all()
    db.query(CanonicalRecord).filter(CanonicalRecord.project_id==project_id,CanonicalRecord.record_type=="ASSESSMENT").delete()
    summary={"AUTO":0,"REVIEW":0,"MANUAL":0}
    for o in objs:
        status="AUTO"; reason="Deterministic conversion candidate"
        if o.object_type=="TRIGGER": status="MANUAL"; reason="Trigger requires architectural review"
        elif o.object_type=="PROCEDURE":
            intent,target=classify_procedure(o.definition or "")
            status="REVIEW" if intent in {"MANUAL_REVIEW","OPERATIONAL_TRANSACTION","UNSUPPORTED"} else "AUTO"
            reason=f"{intent} → {target}"
        elif o.object_type=="FUNCTION":
            intent,target=classify_function(o.definition or ""); reason=f"{intent} → {target}"
        payload={"title":f"{o.schema_name}.{o.object_name}","status":status,"details":{"object_type":o.object_type,"reason":reason}}
        db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="ASSESSMENT",object_id=o.id,payload_json=json.dumps(payload)))
        summary[status]+=1
    db.commit();return {"assessed":len(objs),"summary":summary}

@router.post("/projects/{project_id}/conversion-plans/generate")
def conversion_plan_generate(project_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    objs=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id)).all()
    db.query(CanonicalRecord).filter(CanonicalRecord.project_id==project_id,CanonicalRecord.record_type=="CONVERSION_PLAN").delete()
    n=0
    for o in objs:
        cls=db.scalar(select(MigrationClassification).where(MigrationClassification.project_id==project_id,MigrationClassification.object_id==o.id))
        strategy="DELTA_TABLE" if o.object_type=="TABLE" else "DATABRICKS_SQL"
        review=False
        if o.object_type=="PROCEDURE": _,strategy=classify_procedure(o.definition or "")
        if o.object_type=="FUNCTION": _,strategy=classify_function(o.definition or "")
        if o.object_type=="TRIGGER": _,strategy=classify_trigger(o.definition or ""); review=True
        payload={"title":f"{o.schema_name}.{o.object_name}","status":"REVIEW_REQUIRED" if review else "PLANNED","details":{"object_type":o.object_type,"layer":cls.selected_layer if cls else None,"strategy":strategy}}
        db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="CONVERSION_PLAN",object_id=o.id,payload_json=json.dumps(payload)));n+=1
    db.commit();return {"planned":n}

@router.get("/users")
def users_list(db:Session=Depends(get_db),_=Depends(auth)):
    return [{"id":u.id,"username":u.username,"role":u.role,"locked":u.locked,"failed_attempts":u.failed_attempts} for u in db.scalars(select(User).order_by(User.username)).all()]

@router.post("/users/{user_id}/unlock")
def user_unlock(user_id:str,db:Session=Depends(get_db),_=Depends(auth)):
    u=db.get(User,user_id)
    if not u: raise HTTPException(404,"User not found")
    u.locked=False;u.failed_attempts=0;db.commit();return {"unlocked":True,"username":u.username}

@router.get("/system/diagnostics")
def system_diagnostics(_=Depends(auth)):
    cfg=get_settings(); drivers=[]
    try:
        import pyodbc; drivers=pyodbc.drivers()
    except Exception: pass
    ai=provider_status()
    return {"environment":cfg.environment,"database_url":"configured","sqlserver_driver":cfg.sqlserver_driver,"odbc_drivers":drivers,"sqlserver_auth_mode":"SQL_LOGIN" if cfg.sqlserver_username else "WINDOWS_TRUSTED","databricks_configured":bool(cfg.databricks_host and cfg.databricks_http_path and cfg.databricks_token),"llm_enabled":cfg.llm_enabled,"llm_configured":ai["configured"],"llm_ready_config":ai["ready"],"llm_provider":ai["provider"],"llm_base_url":ai["base_url"],"llm_model":ai["model"],"llm_api_key_required":ai["api_key_required"]}


@router.post("/system/databricks-test")
def databricks_test(_=Depends(auth)):
    try:
        rows=execute_sql("SELECT current_catalog(), current_schema(), current_user()",safe_retry=True)
        return {"ok":True,"result":[list(r) for r in rows]}
    except Exception as e:
        raise HTTPException(400,f"Databricks connection test failed: {e}")
