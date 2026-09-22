from __future__ import annotations
import hashlib, json, re, uuid
from collections import defaultdict, deque
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.models.entities import *
from app.models.canonical import MigrationValidation
from .rules import map_sqlserver_type, map_source_type, classify_layer, classify_procedure, classify_function, classify_trigger, rewrite_common_tsql, rewrite_common_oracle

def uid(prefix: str) -> str: return f"{prefix}_{uuid.uuid4().hex}"
def sha(text: str) -> str: return hashlib.sha256(text.encode()).hexdigest()
def qident(v: str) -> str: return "`" + v.replace("`","``") + "`"


def normalize_databricks_routine_contract(content: str, object_type: str) -> str:
    """Apply safe, deterministic Databricks clauses without changing routine logic."""
    kind = object_type.upper()
    if kind == "PROCEDURE":
        if not re.search(r"(?is)\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?PROCEDURE\b", content):
            return content
        if re.search(r"(?is)\bSQL\s+SECURITY\s+(?:INVOKER|DEFINER)\b", content):
            return content
        language = re.search(r"(?is)\bLANGUAGE\s+SQL\b", content)
        if not language:
            return content
        remainder = content[language.end():].lstrip()
        return content[:language.end()] + "\nSQL SECURITY INVOKER\n" + remainder

    if kind == "FUNCTION":
        if not re.search(r"(?is)\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?FUNCTION\b", content):
            return content
        reads_data = bool(re.search(r"(?is)\b(?:FROM|JOIN)\b", content))
        has_contains = bool(re.search(r"(?is)\bCONTAINS\s+SQL\b", content))
        has_reads = bool(re.search(r"(?is)\bREADS\s+SQL\s+DATA\b", content))

        if reads_data and has_contains:
            if has_reads:
                content = re.sub(r"(?is)\bCONTAINS\s+SQL\s*\n?", "", content)
            else:
                content = re.sub(r"(?is)\bCONTAINS\s+SQL\b", "READS SQL DATA", content)
        elif reads_data and not has_reads:
            language = re.search(r"(?is)\bLANGUAGE\s+SQL\b", content)
            if language:
                remainder = content[language.end():].lstrip()
                content = content[:language.end()] + "\nREADS SQL DATA\n" + remainder
        return content

    return content


def databricks_routine_contract_issues(content: str, object_type: str) -> list[str]:
    """Validate target-runtime clauses that local source checks cannot discover."""
    kind = object_type.upper()
    if kind not in {"PROCEDURE", "FUNCTION"}:
        return []
    issues: list[str] = []
    header = re.search(rf"(?is)\bCREATE\s+OR\s+REPLACE\s+{kind}\b", content)
    language = re.search(r"(?is)\bLANGUAGE\s+SQL\b", content)
    if not header:
        issues.append(f"Databricks {kind.lower()} must use CREATE OR REPLACE {kind}")
    if not language:
        issues.append(f"Databricks {kind.lower()} is missing LANGUAGE SQL")
    if kind == "PROCEDURE":
        security = re.search(r"(?is)\bSQL\s+SECURITY\s+(?:INVOKER|DEFINER)\b", content)
        if not security:
            issues.append("Databricks procedure is missing SQL SECURITY INVOKER")
        elif language and security.start() < language.end():
            issues.append("Databricks procedure SQL SECURITY clause must follow LANGUAGE SQL")
    elif kind == "FUNCTION":
        has_contains = bool(re.search(r"(?is)\bCONTAINS\s+SQL\b", content))
        has_reads = bool(re.search(r"(?is)\bREADS\s+SQL\s+DATA\b", content))
        reads_data = bool(re.search(r"(?is)\b(?:FROM|JOIN)\b", content))
        if has_contains and reads_data:
            issues.append(
                "Databricks SQL function that accesses a table/view cannot specify CONTAINS SQL; use READS SQL DATA instead"
            )
        if has_contains and has_reads:
            issues.append("Databricks SQL function cannot specify both CONTAINS SQL and READS SQL DATA")
        data_clause = re.search(r"(?is)\b(?:CONTAINS\s+SQL|READS\s+SQL\s+DATA)\b", content)
        if data_clause and language and data_clause.start() < language.end():
            issues.append("Databricks function data access clause must follow LANGUAGE SQL")
    return issues


def _retarget_view_header(content: str, target_fqn: str) -> str:
    """Make a discovered view definition idempotent and target the governed DEV FQN.

    Handles SQL Server CREATE VIEW, PostgreSQL CREATE VIEW, and raw SELECT/WITH view bodies.
    """
    cleaned = content.strip()
    pattern = r"(?is)\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?VIEW\s+[^\s(]+"
    match = re.search(pattern, cleaned)
    if match:
        return f"CREATE OR REPLACE VIEW {target_fqn}" + cleaned[match.end():]
    if re.match(r"(?is)^(?:SELECT|WITH)\b", cleaned):
        return f"CREATE OR REPLACE VIEW {target_fqn} AS\n{cleaned}"
    return f"CREATE OR REPLACE VIEW {target_fqn} AS\n{cleaned}"

def ensure_project(db: Session, name: str) -> MigrationProject:
    p = db.scalar(select(MigrationProject).where(MigrationProject.name==name))
    if p: return p
    p=MigrationProject(id=uid("PRJ"),name=name); db.add(p); db.commit(); return p

def add_source(db: Session, project_id: str, profile_name: str, server: str, database: str) -> MigrationSource:
    s=MigrationSource(id=uid("SRC"),project_id=project_id,profile_name=profile_name,server_name=server,database_name=database)
    db.add(s); db.commit(); return s

def ingest_snapshot(db: Session, project_id: str, source_id: str, snapshot: dict) -> dict:
    if not db.get(MigrationProject, project_id): raise ValueError("Unknown project_id")
    counts=defaultdict(int)
    for o in snapshot.get("objects",[]):
        schema_name = str(o.get("schema") or "").strip()
        if not schema_name:
            raise ValueError(f"Source object {o.get('name') or '<unknown>'} is missing explicit schema metadata")
        key = dict(project_id=project_id, source_id=source_id, database_name=o.get("database") or snapshot.get("database",""), schema_name=schema_name, object_name=o["name"], object_type=o["type"].upper())
        obj=db.scalar(select(MigrationObject).where(*(getattr(MigrationObject,k)==v for k,v in key.items())))
        definition=o.get("definition") or ""; source_hash=sha(json.dumps(o,sort_keys=True,default=str))
        if not obj:
            obj=MigrationObject(id=uid("OBJ"),definition=definition,source_hash=source_hash,**key); db.add(obj); db.flush()
        else:
            if obj.source_hash != source_hash:
                db.add(MigrationSchemaDrift(id=uid("DRF"),project_id=project_id,object_id=obj.id,drift_type="DEFINITION_OR_SCHEMA_CHANGE",severity="POTENTIALLY_BREAKING",details="Source object hash changed"))
            obj.definition=definition; obj.source_hash=source_hash
            db.query(MigrationColumn).filter(MigrationColumn.project_id==project_id,MigrationColumn.object_id==obj.id).delete()
            db.query(MigrationDependency).filter(MigrationDependency.project_id==project_id,MigrationDependency.object_id==obj.id).delete()
            db.query(CanonicalRecord).filter(CanonicalRecord.project_id==project_id,CanonicalRecord.object_id==obj.id,CanonicalRecord.record_type.in_(["PARAMETER","COLUMN_TYPE_METADATA","CONSTRAINT","TABLE_STATS"])).delete(synchronize_session=False)
        for i,c in enumerate(o.get("columns",[]),1):
            db.add(MigrationColumn(id=uid("COL"),project_id=project_id,object_id=obj.id,column_name=c["name"],ordinal=c.get("ordinal",i),data_type=c["type"],max_length=c.get("max_length"),precision=c.get("precision"),scale=c.get("scale"),nullable=c.get("nullable",True),is_identity=c.get("identity",False),is_computed=c.get("computed",False),default_definition=c.get("default")))
            # Preserve declared/user-defined type metadata without changing the existing
            # migration_column schema. Runtime adapters use the underlying system type,
            # while governance can still inspect the original alias/UDT declaration.
            declared=c.get("declared_type")
            system_type=c.get("system_type")
            if declared or c.get("is_user_defined"):
                db.add(CanonicalRecord(
                    id=uid("REC"), project_id=project_id, record_type="COLUMN_TYPE_METADATA", object_id=obj.id,
                    payload_json=json.dumps({
                        "column":c["name"],"ordinal":c.get("ordinal",i),"declared_type":declared or c.get("type"),
                        "system_type":system_type or c.get("type"),"effective_type":c.get("type"),
                        "is_user_defined":bool(c.get("is_user_defined")),
                    },default=str),
                ))
        for d in o.get("dependencies",[]):
            db.add(MigrationDependency(id=uid("DEP"),project_id=project_id,object_id=obj.id,referenced_database=d.get("database"),referenced_schema=d.get("schema"),referenced_object=d.get("object") or "",referenced_column=d.get("column"),dependency_type=d.get("type") or "LOCAL"))
        for p in o.get("parameters",[]):
            db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="PARAMETER",object_id=obj.id,payload_json=json.dumps(p,default=str)))
        for constraint in o.get("constraints",[]):
            db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="CONSTRAINT",object_id=obj.id,payload_json=json.dumps(constraint,default=str)))
        if o.get("approx_row_count") is not None:
            db.add(CanonicalRecord(id=uid("REC"),project_id=project_id,record_type="TABLE_STATS",object_id=obj.id,payload_json=json.dumps({"approx_row_count":int(o.get("approx_row_count") or 0)},default=str)))
        counts[o["type"].upper()] += 1
    db.commit(); return dict(counts)

def classify_project(db: Session, project_id: str) -> int:
    objects=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id)).all(); n=0
    for o in objects:
        layer,conf,reason=classify_layer(o.object_type,o.definition,o.object_name)
        c=db.scalar(select(MigrationClassification).where(MigrationClassification.project_id==project_id,MigrationClassification.object_id==o.id))
        if not c:
            c=MigrationClassification(id=uid("CLS"),project_id=project_id,object_id=o.id,recommended_layer=layer,selected_layer=layer,classification_reason=reason,confidence_score=conf,classification_method="DETERMINISTIC_RULES"); db.add(c)
        else:
            c.recommended_layer=layer; c.classification_reason=reason; c.confidence_score=conf
        n+=1
    db.commit(); return n

def create_mappings(db: Session, project_id: str, environment: str, catalog: str) -> int:
    objs=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id)).all(); n=0
    for o in objs:
        cls=db.scalar(select(MigrationClassification).where(MigrationClassification.project_id==project_id,MigrationClassification.object_id==o.id))
        if not cls: continue
        source_fqn=f"{o.database_name}.{o.schema_name}.{o.object_name}"; target=f"{qident(catalog)}.{qident(cls.selected_layer.lower())}.{qident(o.object_name)}"
        m=db.scalar(select(MigrationMapping).where(MigrationMapping.project_id==project_id,MigrationMapping.object_id==o.id,MigrationMapping.environment==environment.upper()))
        if not m: db.add(MigrationMapping(id=uid("MAP"),project_id=project_id,object_id=o.id,source_fqn=source_fqn,target_fqn=target,target_layer=cls.selected_layer,environment=environment.upper()))
        else: m.target_fqn=target; m.target_layer=cls.selected_layer
        n+=1
    db.commit(); return n


def _parse_params_from_definition(definition: str) -> list[dict]:
    m = re.search(r"\b(?:PROCEDURE|FUNCTION)\s+[\w\.\"`\[\]]+\s*\(([\s\S]*?)\)", definition or "", flags=re.I)
    if not m:
        return []
    param_str = m.group(1).strip()
    if not param_str:
        return []
    params = []
    raw_params = [p.strip() for p in re.split(r",(?![^\(]*\))", param_str) if p.strip()]
    for idx, p in enumerate(raw_params, 1):
        tokens = p.split()
        if not tokens:
            continue
        is_output = False
        mode = "IN"
        if tokens[0].upper() in {"IN", "OUT", "INOUT", "VARIADIC"}:
            mode = tokens[0].upper()
            is_output = mode in {"OUT", "INOUT"}
            tokens = tokens[1:]
        if not tokens:
            continue
        param_name = tokens[0].strip('"`[]')
        type_str = " ".join(tokens[1:]) if len(tokens) > 1 else "string"
        t_match = re.match(r"^([^\(]+)(?:\((\d+)(?:,\s*(\d+))?\))?", type_str)
        if t_match:
            base_type = t_match.group(1).strip()
            prec = int(t_match.group(2)) if t_match.group(2) else None
            scale = int(t_match.group(3)) if t_match.group(3) else None
        else:
            base_type = type_str
            prec = None
            scale = None
        params.append({
            "name": param_name,
            "ordinal": idx,
            "type": base_type,
            "precision": prec,
            "scale": scale,
            "is_output": is_output
        })
    return params


def _routine_parameters(db: Session, project_id: str, object_id: str) -> list[dict]:
    rows=db.scalars(select(CanonicalRecord).where(
        CanonicalRecord.project_id==project_id,
        CanonicalRecord.object_id==object_id,
        CanonicalRecord.record_type=="PARAMETER",
    )).all()
    out=[]
    for row in rows:
        try:
            payload=json.loads(row.payload_json or "{}")
        except Exception:
            payload={}
        if payload:
            out.append(payload)
    if out:
        return sorted(out,key=lambda x:int(x.get("ordinal") or 0))
    obj = db.get(MigrationObject, object_id)
    if obj and obj.definition:
        return _parse_params_from_definition(obj.definition)
    return []


def _parameter_signature(params: list[dict], *, procedure: bool=False) -> str:
    parts=[]
    for p in params:
        raw=str(p.get("name") or "").strip()
        if not raw or raw.lower() in {"@return_value","return_value"}:
            continue
        name=re.sub(r"^@+","",raw)
        dtype=map_source_type(str(p.get("type") or "string"),p.get("precision"),p.get("scale"), source_type="ORACLE")
        mode="OUT " if procedure and p.get("is_output") else ("IN " if procedure else "")
        parts.append(f"{mode}{qident(name)} {dtype}")
    return ", ".join(parts)


def _replace_parameters(text: str, params: list[dict]) -> str:
    out=text
    for p in params:
        raw=str(p.get("name") or "").strip()
        if raw.startswith("@"):
            out=re.sub(rf"(?<![\w@]){re.escape(raw)}\b",qident(raw[1:]),out,flags=re.I)
    return out


def _replace_known_references(db: Session, project_id: str, environment: str, content: str) -> str:
    mappings=db.scalars(select(MigrationMapping).where(
        MigrationMapping.project_id==project_id,
        MigrationMapping.environment==environment.upper(),
    )).all()
    out=content
    for x in sorted(mappings,key=lambda z:len(z.source_fqn),reverse=True):
        parts=x.source_fqn.split(".",2)
        if len(parts)!=3:
            continue
        _,sch,name=parts
        patterns=[
            rf"(?<![\w`\"\[])\[?{re.escape(sch)}\]?\.\[?{re.escape(name)}\]?(?![\w`\"\]])",
            rf"(?<![\w`\"\[])`?{re.escape(sch)}`?\.`?{re.escape(name)}`?(?![\w`\"\]])",
            rf"(?<![\w`\"\[])\"?{re.escape(sch)}\"?\.\"?{re.escape(name)}\"?(?![\w`\"\]])",
        ]
        for pat in patterns:
            out=re.sub(pat,lambda _: x.target_fqn,out,flags=re.I)
        for keyword in ("FROM", "JOIN", "INTO", "UPDATE"):
            pat = rf"(?i)\b({keyword}\s+)(?:\[?{re.escape(name)}\]?|`{re.escape(name)}`|\"{re.escape(name)}\")(?![\w`\.])"
            out = re.sub(pat, lambda m: f"{m.group(1)}{x.target_fqn}", out, flags=re.I)
    return out


def _clean_routine_body(definition: str) -> str:
    # 1. Handle PostgreSQL dollar quoting ($tag$ ... $tag$ or $$ ... $$) or single quote (' ... ')
    dollar_match = re.search(r"\bAS\s+(\$([A-Za-z0-9_]*)\$)([\s\S]*?)\1\s*;?\s*$", definition or "", flags=re.I)
    if dollar_match:
        body = dollar_match.group(3).strip()
    else:
        # Fallback to AS/IS match (T-SQL, PL/SQL)
        m = re.search(r"\b(?:AS|IS)\b(.*)$", definition or "", flags=re.I | re.S)
        body = (m.group(1) if m else definition or "").strip()
        if body.startswith("$$") and body.endswith("$$"):
            body = body[2:-2].strip()
        elif body.startswith("'") and body.endswith("'"):
            body = body[1:-1].strip()

    # 2. Strip standard BEGIN ... END wrapper if present
    body = re.sub(r"^\s*BEGIN\b", "", body, flags=re.I).strip()
    body = re.sub(r"(?is)\bGO\s*;?\s*$", "", body).strip()
    body = re.sub(r"(?is)\bEND\s*(?:[A-Za-z_]\w*)?\s*;?\s*$", "", body).strip()
    body = re.sub(r"\bSET\s+NOCOUNT\s+ON\s*;?", "", body, flags=re.I)
    body = re.sub(r"\bSET\s+ANSI_NULLS\s+(?:ON|OFF)\s*;?", "", body, flags=re.I)
    body = re.sub(r"\bSET\s+QUOTED_IDENTIFIER\s+(?:ON|OFF)\s*;?", "", body, flags=re.I)
    # Also strip any leftover dollar tag in case of non-standard end
    body = re.sub(r"^\s*\$[A-Za-z0-9_]*\$\s*", "", body, flags=re.I)
    body = re.sub(r"\s*\$[A-Za-z0-9_]*\$\s*;?\s*$", "", body, flags=re.I)
    body = re.sub(r"^\s*BEGIN\b", "", body, flags=re.I).strip()
    body = re.sub(r"(?is)\bEND\s*(?:[A-Za-z_]\w*)?\s*;?\s*$", "", body).strip()
    return body.strip()


def _rewrite_static_procedure_calls(body: str) -> str:
    """Translate mapped, static T-SQL EXEC calls into Databricks SQL CALL syntax.

    Dynamic EXEC remains untouched and is rejected by the procedure compatibility guard.
    At this stage known source procedure names have already been replaced by quoted
    three-part Databricks identifiers, which makes the bounded rewrite unambiguous.
    """
    pattern = r"(?im)\bEXEC(?:UTE)?\s+(`[^`]+`\.`[^`]+`\.`[^`]+`)\s*;"
    return re.sub(pattern, lambda match: f"CALL {match.group(1)}();", body)


def _convert_function(db: Session, project_id: str, o: MigrationObject, m: MigrationMapping, environment: str) -> tuple[str,bool,str]:
    definition=o.definition or ""
    params=_routine_parameters(db,project_id,o.id)
    sig=_parameter_signature(params)
    ft,target=classify_function(definition)
    rewritten=_replace_known_references(db,project_id,environment,_replace_parameters(rewrite_common_tsql(definition),params))

    # Inline table-valued function: RETURN (SELECT ...)
    if ft=="INLINE_TVF":
        mm=re.search(r"\bRETURN\s*\((.*)\)\s*;?\s*$",_clean_routine_body(rewritten),flags=re.I|re.S)
        query=(mm.group(1).strip() if mm else "")
        if query and re.match(r"(?is)^\s*(SELECT|WITH)\b",query):
            content = f"CREATE OR REPLACE FUNCTION {m.target_fqn}({sig})\nRETURNS TABLE\nLANGUAGE SQL\nRETURN ({query.rstrip(';')});"
            content = normalize_databricks_routine_contract(content, "FUNCTION")
            return (content,True,"INLINE_TVF_TO_SQL_TABLE_FUNCTION")

    # Scalar function with deterministic RETURN expression/query.
    ret_type_match=re.search(r"\bRETURNS\s+([\[\]\w]+)(?:\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?",definition,flags=re.I)
    source_ret=(ret_type_match.group(1).strip('[]') if ret_type_match else "string")
    precision=int(ret_type_match.group(2)) if ret_type_match and ret_type_match.group(2) else None
    scale=int(ret_type_match.group(3)) if ret_type_match and ret_type_match.group(3) else None
    ret_type=map_sqlserver_type(source_ret,precision,scale)
    body=_clean_routine_body(rewritten)
    mm=re.search(r"\bRETURN\s+(.+?)(?:;\s*$|$)",body,flags=re.I|re.S)
    expr=(mm.group(1).strip() if mm else "")
    # Avoid auto-converting multi-statement/stateful UDFs.
    complex_tokens=("declare "," set ","while ","cursor ","insert ","update ","delete ","merge ","raiserror","throw ")
    if expr and not any(tok in body.lower() for tok in complex_tokens):
        content = f"CREATE OR REPLACE FUNCTION {m.target_fqn}({sig})\nRETURNS {ret_type}\nLANGUAGE SQL\nRETURN {expr.rstrip(';')};"
        content = normalize_databricks_routine_contract(content, "FUNCTION")
        return (content,True,"SCALAR_UDF_TO_SQL_FUNCTION")

    reason=f"{ft} requires semantic/manual remediation before executable deployment ({target})."
    return (f"-- FUNCTION_CLASSIFICATION: {ft}\n-- RECOMMENDED_TARGET: {target}\n-- NON_EXECUTABLE: {reason}\n"+rewritten,False,reason)


def _convert_procedure(db: Session, project_id: str, o: MigrationObject, m: MigrationMapping, environment: str) -> tuple[str,bool,str]:
    definition=o.definition or ""
    params=_routine_parameters(db,project_id,o.id)
    sig=_parameter_signature(params,procedure=True)
    intent,target=classify_procedure(definition)
    body=_clean_routine_body(definition)
    body=_replace_parameters(rewrite_common_tsql(body),params)
    body=_replace_known_references(db,project_id,environment,body)
    body=_rewrite_static_procedure_calls(body)
    low=body.lower()

    unsupported=("sp_executesql","openquery(","xp_","raiserror","throw ")
    if any(x in low for x in unsupported) or re.search(r"(?i)\bEXEC(?:UTE)?\b", body):
        reason=f"{intent} contains unsupported dynamic/external behavior; deterministic conversion stopped safely."
        return (f"-- PROCEDURE_CLASSIFICATION: {intent}\n-- RECOMMENDED_TARGET: {target}\n-- NON_EXECUTABLE: {reason}\n"+body,False,reason)

    # Clean transaction and try-catch wrappers: Databricks Delta Lake is ACID by default
    clean_body = re.sub(r"(?is)\bBEGIN\s+TRAN(?:SACTION)?\s*;?", "", body)
    clean_body = re.sub(r"(?is)\bCOMMIT\s+TRAN(?:SACTION)?\s*;?", "", clean_body)
    clean_body = re.sub(r"(?is)\bCOMMIT\s*;?", "", clean_body)
    clean_body = re.sub(r"(?is)\bROLLBACK\s+TRAN(?:SACTION)?\s*;?", "", clean_body)
    clean_body = re.sub(r"(?is)\bBEGIN\s+TRY\b\s*;?", "", clean_body)
    clean_body = re.sub(r"(?is)\bEND\s+TRY\b\s*;?", "", clean_body)
    clean_body = re.sub(r"(?is)\bBEGIN\s+CATCH\b[\s\S]*?\bEND\s+CATCH\b\s*;?", "", clean_body)
    clean_body = clean_body.strip()

    low_clean = clean_body.lower()
    incompatible=("goto ","waitfor ")
    if any(x in low_clean for x in incompatible):
        reason=f"{intent} contains control-flow constructs requiring SQL scripting remediation."
        return (f"-- PROCEDURE_CLASSIFICATION: {intent}\n-- RECOMMENDED_TARGET: {target}\n-- NON_EXECUTABLE: {reason}\n"+body,False,reason)

    if clean_body:
        content=(f"CREATE OR REPLACE PROCEDURE {m.target_fqn}({sig})\n"
                 f"LANGUAGE SQL\nSQL SECURITY INVOKER\nAS BEGIN\n{clean_body.rstrip(';')};\nEND;")
        content = normalize_databricks_routine_contract(content, "PROCEDURE")
        return (content,True,f"{intent}_TO_DATABRICKS_SQL_PROCEDURE")
    reason="Procedure body could not be parsed safely."
    return (f"-- PROCEDURE_CLASSIFICATION: {intent}\n-- NON_EXECUTABLE: {reason}\n"+definition,False,reason)

def generate_artifact(db: Session, project_id: str, object_id: str, environment: str="DEV") -> MigrationArtifactVersion:
    o=db.get(MigrationObject,object_id)
    if not o or o.project_id!=project_id: raise ValueError("Object not found in project")
    m=db.scalar(select(MigrationMapping).where(MigrationMapping.project_id==project_id,MigrationMapping.object_id==object_id,MigrationMapping.environment==environment.upper()))
    if not m: raise ValueError("Mapping missing")
    cols=db.scalars(select(MigrationColumn).where(MigrationColumn.project_id==project_id,MigrationColumn.object_id==object_id).order_by(MigrationColumn.ordinal)).all()
    if o.object_type=="TABLE":
        defs=[f"  {qident(c.column_name)} {map_sqlserver_type(c.data_type,c.precision,c.scale)}" + (" NOT NULL" if not c.nullable else "") for c in cols]
        content=f"CREATE TABLE {m.target_fqn} (\n" + ",\n".join(defs + ["  `_migration_ingested_at` TIMESTAMP", "  `_migration_source_system` STRING"]) + "\n) USING DELTA;"
    elif o.object_type=="PROCEDURE":
        content,_,_= _convert_procedure(db,project_id,o,m,environment)
    elif o.object_type=="FUNCTION":
        content,_,_= _convert_function(db,project_id,o,m,environment)
    elif o.object_type=="TRIGGER":
        intent,target=classify_trigger(o.definition or ""); content=f"-- TRIGGER_INTENT: {intent}\n-- RECOMMENDED_TARGET: {target}\n-- ARCHITECT_REVIEW_REQUIRED\n"+(o.definition or "")
    else:
        content=rewrite_common_tsql(o.definition or "")
        # parser-assisted bounded reference replacement; only known object mappings are replaced
        content=_replace_known_references(db,project_id,environment,content)
        if o.object_type=="VIEW":
            # Reference rewriting may already have replaced the source view identifier,
            # so canonicalize the whole header rather than matching the old name again.
            content=_retarget_view_header(content,m.target_fqn)
    art=db.scalar(select(MigrationArtifact).where(MigrationArtifact.project_id==project_id,MigrationArtifact.object_id==object_id))
    if not art:
        art=MigrationArtifact(id=uid("ART"),project_id=project_id,object_id=object_id,artifact_type=o.object_type,current_version=0); db.add(art); db.flush()
    version=art.current_version+1
    av=MigrationArtifactVersion(
        id=uid("ARV"), project_id=project_id, artifact_id=art.id, version=version, content=content,
        source_hash=o.source_hash or sha(o.definition or ""), target_hash=sha(content),
        generator_version="enterprise-2.3.0", rule_version="rules-2.3-semantic-medallion",
    ); db.add(av); art.current_version=version
    db.commit(); return av

def static_validate(db: Session, project_id: str, object_id: str, environment: str="DEV") -> dict:
    o=db.get(MigrationObject,object_id)
    if not o or o.project_id!=project_id: raise ValueError("Object not found")
    definition=o.definition or ""; issues=[]
    aliases={m.group(3).lower(): (m.group(1),m.group(2)) for m in re.finditer(r"(?:from|join)\s+(?:\[?([\w]+)\]?\.)?\[?([\w]+)\]?\s+(?:as\s+)?([A-Za-z_]\w*)",definition,re.I)}
    # Best-effort alias.column validation against discovered objects/columns; no invented columns.
    for a,col in re.findall(r"\b([A-Za-z_]\w*)\.\[?([A-Za-z_]\w*)\]?",definition):
        if a.lower() in aliases:
            sch,objname=aliases[a.lower()]
            candidates=db.scalars(select(MigrationObject).where(MigrationObject.project_id==project_id,MigrationObject.object_name==objname)).all()
            target=next((x for x in candidates if not sch or x.schema_name.lower()==sch.lower()),None)
            if target:
                exists=db.scalar(select(MigrationColumn).where(MigrationColumn.project_id==project_id,MigrationColumn.object_id==target.id,MigrationColumn.column_name.ilike(col)))
                if not exists:
                    issue=MigrationIssue(id=uid("ISS"),project_id=project_id,object_id=object_id,issue_type="UNRESOLVED_COLUMN",severity="BLOCKER",message=f"Unresolved column {a}.{col}",recommended_action="Correct source logic or mapping before deployment")
                    db.add(issue); issues.append(issue.message)

    art=db.scalar(select(MigrationArtifact).where(MigrationArtifact.project_id==project_id,MigrationArtifact.object_id==object_id))
    av=None
    if art:
        av=db.scalar(select(MigrationArtifactVersion).where(
            MigrationArtifactVersion.project_id==project_id,
            MigrationArtifactVersion.artifact_id==art.id,
            MigrationArtifactVersion.version==art.current_version,
        ))
    if not av:
        issues.append("No current artifact version exists for static validation")
    else:
        if "-- NON_EXECUTABLE:" in av.content.upper() or "ARCHITECT_REVIEW_REQUIRED" in av.content.upper():
            issues.append("Current artifact is not executable and requires remediation before approval")
        issues.extend(databricks_routine_contract_issues(av.content, o.object_type))

    status="PASSED" if not issues else "FAILED"
    db.add(MigrationValidation(
        id=uid("VAL"), project_id=project_id, object_id=object_id, environment=environment.upper(), status=status,
        payload_json=json.dumps({
            "artifact_version_id": av.id if av else None,
            "artifact_version": av.version if av else None,
            "validation_type": "STATIC_VALIDATION",
            "issues": issues,
        }, default=str),
    ))
    db.commit(); return {"valid":not issues,"status":status,"artifact_version_id":av.id if av else None,"issues":issues}

def topo_order(edges: list[tuple[str,str]]) -> list[str]:
    nodes=set(); indeg=defaultdict(int); out=defaultdict(list)
    for obj,dep in edges:
        nodes|={obj,dep}; out[dep].append(obj); indeg[obj]+=1; indeg.setdefault(dep,0)
    q=deque(sorted([n for n in nodes if indeg[n]==0])); result=[]
    while q:
        n=q.popleft(); result.append(n)
        for x in out[n]:
            indeg[x]-=1
            if indeg[x]==0:q.append(x)
    if len(result)!=len(nodes): raise ValueError("Circular dependency detected")
    return result

def compare_schema(expected: list[dict], actual: list[dict]) -> dict:
    e={x["name"].lower():x for x in expected}; a={x["name"].lower():x for x in actual}; changes=[]; breaking=False
    for k,v in e.items():
        if k not in a: changes.append({"type":"ADD_COLUMN","column":v["name"],"severity":"NON_BREAKING"})
        elif str(v["type"]).upper()!=str(a[k]["type"]).upper(): changes.append({"type":"TYPE_CHANGE","column":v["name"],"severity":"BREAKING"}); breaking=True
    for k,v in a.items():
        if k not in e: changes.append({"type":"REMOVE_TARGET_COLUMN","column":v["name"],"severity":"POTENTIALLY_BREAKING"})
    return {"status":"IDENTICAL" if not changes else ("BREAKING" if breaking else "NON_BREAKING_CHANGE"),"changes":changes}

def lifecycle(db: Session, project_id: str) -> list[dict]:
    rows=[]
    for env in ["DEV","TEST","UAT","PROD"]:
        gate=db.scalars(select(MigrationQualityGate).where(MigrationQualityGate.project_id==project_id,MigrationQualityGate.environment==env).order_by(MigrationQualityGate.created_at.desc())).first()
        if gate:
            run=db.get(MigrationRun,gate.run_id)
            rows.append({"environment":env,"status":gate.status,"run_id":gate.run_id,"start_time":run.started_at if run else None,"end_time":run.ended_at if run else None,"pass_count":gate.pass_count,"fail_count":gate.fail_count,"review_blockers":gate.blocker_count,"deployment_version":gate.deployment_version})
        else: rows.append({"environment":env,"status":"NOT_STARTED","run_id":None,"pass_count":0,"fail_count":0,"review_blockers":0,"deployment_version":None})
    return rows

def override_layer(db: Session, project_id: str, object_id: str, selected_layer: str, user: str, reason: str) -> MigrationClassification:
    layer=selected_layer.upper()
    if layer not in {"BRONZE","SILVER","GOLD"}: raise ValueError("Layer must be BRONZE, SILVER or GOLD")
    c=db.scalar(select(MigrationClassification).where(MigrationClassification.project_id==project_id,MigrationClassification.object_id==object_id))
    if not c: raise ValueError("Classification not found")
    c.selected_layer=layer;c.override_user=user;c.override_reason=reason;c.classification_method="USER_OVERRIDE";db.commit();return c

def promotion_precheck(db: Session, project_id: str, target_environment: str) -> dict:
    target=target_environment.upper(); previous={"TEST":"DEV","UAT":"TEST","PROD":"UAT"}.get(target)
    blockers=[]
    if previous:
        g=db.scalars(select(MigrationQualityGate).where(MigrationQualityGate.project_id==project_id,MigrationQualityGate.environment==previous).order_by(MigrationQualityGate.created_at.desc())).first()
        if not g or g.status!="PASSED": blockers.append(f"{previous} quality gate is not PASSED")
    artifacts=db.scalars(select(MigrationArtifact).where(MigrationArtifact.project_id==project_id)).all()
    for a in artifacts:
        av=db.scalar(select(MigrationArtifactVersion).where(MigrationArtifactVersion.project_id==project_id,MigrationArtifactVersion.artifact_id==a.id,MigrationArtifactVersion.version==a.current_version))
        if not av: blockers.append(f"Artifact {a.id} has no current version"); continue
        review=db.scalars(select(MigrationReview).where(
            MigrationReview.project_id==project_id,
            MigrationReview.artifact_version_id==av.id,
        ).order_by(MigrationReview.reviewed_at.desc())).first()
        if not review or review.status!="APPROVED": blockers.append(f"Artifact version {av.id} is not approved")
    open_blockers=db.scalars(select(MigrationIssue).where(MigrationIssue.project_id==project_id,MigrationIssue.status=="OPEN",MigrationIssue.severity=="BLOCKER")).all()
    if open_blockers: blockers.append(f"{len(open_blockers)} open blocker issue(s)")
    return {"eligible":not blockers,"target_environment":target,"blockers":blockers}
