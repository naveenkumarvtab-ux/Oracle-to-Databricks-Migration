import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ==============================================================================
# POSTGRESQL DISCOVERY SQL
# ==============================================================================

PG_DISCOVERY_SQL = r"""
SELECT 
    current_database() AS database_name,
    n.nspname AS schema_name,
    c.relname AS object_name,
    CASE c.relkind
        WHEN 'r' THEN 'TABLE'
        WHEN 'p' THEN 'TABLE'
        WHEN 'v' THEN 'VIEW'
        WHEN 'm' THEN 'VIEW'
        WHEN 'f' THEN 'FOREIGN_TABLE'
        ELSE 'TABLE'
    END AS object_type,
    pg_get_viewdef(c.oid, true) AS definition
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND c.relkind IN ('r', 'p', 'v', 'm')
UNION ALL
SELECT
    current_database() AS database_name,
    n.nspname AS schema_name,
    p.proname AS object_name,
    CASE p.prokind
        WHEN 'p' THEN 'PROCEDURE'
        WHEN 'f' THEN 'FUNCTION'
        WHEN 'a' THEN 'AGGREGATE'
        WHEN 'w' THEN 'WINDOW_FUNCTION'
        ELSE 'FUNCTION'
    END AS object_type,
    pg_get_functiondef(p.oid) AS definition
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
UNION ALL
SELECT
    current_database() AS database_name,
    n.nspname AS schema_name,
    t.tgname AS object_name,
    'TRIGGER' AS object_type,
    pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND NOT t.tgisinternal;
"""

PG_COLUMN_SQL = r"""
SELECT 
    n.nspname AS schema_name,
    c.relname AS object_name,
    a.attname AS column_name,
    a.attnum AS column_id,
    format_type(a.atttypid, a.atttypmod) AS declared_data_type,
    t.typname AS system_data_type,
    (t.typtype = 'd' OR t.typtype = 'e') AS is_user_defined,
    information_schema._pg_char_max_length(a.atttypid, a.atttypmod) AS max_length,
    information_schema._pg_numeric_precision(a.atttypid, a.atttypmod) AS precision,
    information_schema._pg_numeric_scale(a.atttypid, a.atttypmod) AS scale,
    NOT a.attnotnull AS is_nullable,
    (a.attidentity != '' OR pg_get_expr(ad.adbin, ad.adrelid) LIKE 'nextval(%%') AS is_identity,
    (a.attgenerated != '') AS is_computed,
    pg_get_expr(ad.adbin, ad.adrelid) AS default_definition,
    coll.collname AS collation_name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
LEFT JOIN pg_collation coll ON coll.oid = a.attcollation
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum;
"""

PG_KEY_CONSTRAINT_SQL = r"""
SELECT 
    n.nspname AS schema_name,
    c.relname AS object_name,
    con.conname AS constraint_name,
    CASE con.contype
        WHEN 'p' THEN 'PRIMARY KEY'
        WHEN 'u' THEN 'UNIQUE'
        ELSE con.contype::text
    END AS constraint_type,
    pos.ordinal AS key_ordinal,
    a.attname AS column_name
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS pos(attnum, ordinal)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = pos.attnum
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND con.contype IN ('p', 'u')
ORDER BY n.nspname, c.relname, con.conname, pos.ordinal;
"""

PG_FOREIGN_KEY_SQL = r"""
SELECT 
    n.nspname AS schema_name,
    c.relname AS object_name,
    con.conname AS constraint_name,
    pos.ordinal AS ordinal,
    a.attname AS column_name,
    fn.nspname AS referenced_schema,
    fc.relname AS referenced_object,
    fa.attname AS referenced_column
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class fc ON fc.oid = con.confrelid
JOIN pg_namespace fn ON fn.oid = fc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS pos(attnum, ordinal)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = pos.attnum
CROSS JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS fpos(confattnum, forder)
JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = fpos.confattnum AND pos.ordinal = fpos.forder
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND con.contype = 'f'
ORDER BY n.nspname, c.relname, con.conname, pos.ordinal;
"""

PG_TABLE_STATS_SQL = r"""
SELECT 
    schemaname AS schema_name,
    relname AS object_name,
    n_live_tup AS approx_row_count
FROM pg_stat_user_tables;
"""

PG_DEPENDENCY_SQL = r"""
SELECT 
    n1.nspname AS referencing_schema_name,
    c1.relname AS referencing_entity_name,
    NULL::text AS referenced_server_name,
    current_database() AS referenced_database_name,
    n2.nspname AS referenced_schema_name,
    c2.relname AS referenced_entity_name,
    NULL::text AS referenced_column_name,
    NULL::int AS referenced_minor_id,
    'LOCAL' AS dependency_scope,
    false AS is_schema_bound_reference,
    false AS is_caller_dependent,
    false AS is_ambiguous
FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid
JOIN pg_class c1 ON c1.oid = r.ev_class
JOIN pg_namespace n1 ON n1.oid = c1.relnamespace
JOIN pg_class c2 ON c2.oid = d.refobjid
JOIN pg_namespace n2 ON n2.oid = c2.relnamespace
WHERE n1.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND n2.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND c1.oid != c2.oid;
"""

# ==============================================================================
# SQL SERVER DISCOVERY SQL (Backwards Compatibility)
# ==============================================================================

DISCOVERY_SQL = r"""
SELECT DB_NAME() AS database_name, s.name AS schema_name, o.name AS object_name,
       CASE o.type WHEN 'U' THEN 'TABLE' WHEN 'V' THEN 'VIEW' WHEN 'P' THEN 'PROCEDURE'
                   WHEN 'FN' THEN 'FUNCTION' WHEN 'IF' THEN 'FUNCTION' WHEN 'TF' THEN 'FUNCTION'
                   WHEN 'TR' THEN 'TRIGGER' ELSE o.type_desc END AS object_type,
       m.definition
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id=o.schema_id
LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id
WHERE o.is_ms_shipped=0 AND o.type IN ('U','V','P','FN','IF','TF','TR');
"""
COLUMN_SQL = r"""
SELECT s.name schema_name,o.name object_name,c.name column_name,c.column_id,
       t.name declared_data_type, TYPE_NAME(c.system_type_id) system_data_type, t.is_user_defined,
       c.max_length,c.precision,c.scale,c.is_nullable,c.is_identity,c.is_computed,
       dc.definition default_definition, c.collation_name
FROM sys.objects o JOIN sys.schemas s ON o.schema_id=s.schema_id
JOIN sys.columns c ON c.object_id=o.object_id JOIN sys.types t ON c.user_type_id=t.user_type_id
LEFT JOIN sys.default_constraints dc ON c.default_object_id=dc.object_id
WHERE o.is_ms_shipped=0 AND o.type IN ('U','V');
"""
DEPENDENCY_SQL = r"""
SELECT
    d.referencing_id,
    OBJECT_SCHEMA_NAME(d.referencing_id) AS referencing_schema_name,
    OBJECT_NAME(d.referencing_id) AS referencing_entity_name,
    d.referenced_id,
    d.referenced_server_name,
    d.referenced_database_name,
    d.referenced_schema_name,
    d.referenced_entity_name,
    d.referenced_minor_id,
    c.name AS referenced_column_name,
    CASE
        WHEN d.referenced_server_name IS NOT NULL THEN 'EXTERNAL_SERVER'
        WHEN d.referenced_database_name IS NOT NULL AND d.referenced_database_name <> DB_NAME() THEN 'CROSS_DATABASE'
        WHEN d.referenced_schema_name IS NOT NULL AND d.referenced_schema_name <> OBJECT_SCHEMA_NAME(d.referencing_id) THEN 'CROSS_SCHEMA'
        ELSE 'LOCAL'
    END AS dependency_scope,
    d.is_schema_bound_reference,
    d.is_caller_dependent,
    d.is_ambiguous
FROM sys.sql_expression_dependencies AS d
LEFT JOIN sys.columns AS c
    ON c.object_id = d.referenced_id
   AND c.column_id = d.referenced_minor_id
WHERE d.referenced_entity_name IS NOT NULL;
"""
KEY_CONSTRAINT_SQL = r"""
SELECT s.name AS schema_name, o.name AS object_name, kc.name AS constraint_name,
       kc.type_desc AS constraint_type, ic.key_ordinal, c.name AS column_name
FROM sys.key_constraints kc
JOIN sys.objects o ON o.object_id=kc.parent_object_id
JOIN sys.schemas s ON s.schema_id=o.schema_id
JOIN sys.index_columns ic ON ic.object_id=o.object_id AND ic.index_id=kc.unique_index_id
JOIN sys.columns c ON c.object_id=o.object_id AND c.column_id=ic.column_id
WHERE o.is_ms_shipped=0 AND kc.type IN ('PK','UQ')
ORDER BY s.name,o.name,kc.name,ic.key_ordinal;
"""
FOREIGN_KEY_SQL = r"""
SELECT ps.name AS schema_name, po.name AS object_name, fk.name AS constraint_name,
       fkc.constraint_column_id AS ordinal, pc.name AS column_name,
       rs.name AS referenced_schema, ro.name AS referenced_object, rc.name AS referenced_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
JOIN sys.objects po ON po.object_id=fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id=po.schema_id
JOIN sys.columns pc ON pc.object_id=po.object_id AND pc.column_id=fkc.parent_column_id
JOIN sys.objects ro ON ro.object_id=fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id=ro.schema_id
JOIN sys.columns rc ON rc.object_id=ro.object_id AND rc.column_id=fkc.referenced_column_id
WHERE po.is_ms_shipped=0
ORDER BY ps.name,po.name,fk.name,fkc.constraint_column_id;
"""
TABLE_STATS_SQL = r"""
SELECT s.name AS schema_name,o.name AS object_name,
       SUM(CASE WHEN p.index_id IN (0,1) THEN p.row_count ELSE 0 END) AS approx_row_count
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id=o.schema_id
JOIN sys.dm_db_partition_stats p ON p.object_id=o.object_id
WHERE o.is_ms_shipped=0 AND o.type='U'
GROUP BY s.name,o.name;
"""
PARAMETER_SQL = r"""
SELECT s.name AS schema_name,o.name AS object_name,p.name AS parameter_name,p.parameter_id,
       t.name AS data_type,p.max_length,p.precision,p.scale,p.is_output
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id=o.schema_id
JOIN sys.parameters p ON p.object_id=o.object_id
JOIN sys.types t ON t.user_type_id=p.user_type_id
WHERE o.is_ms_shipped=0 AND o.type IN ('P','FN','IF','TF');
"""

# ==============================================================================
# CONNECTION DIAGNOSTICS & HELPERS
# ==============================================================================

def connection_diagnostic(error: Exception) -> str:
    message = str(error).lower()
    # Oracle specific diagnostics
    if "ora-01017" in message or "invalid username/password" in message:
        return "AUTHENTICATION_FAILED: Verify Oracle username and password."
    if "ora-12541" in message or "no listener" in message or "tns:no listener" in message:
        return "NETWORK_UNREACHABLE: No Oracle TNS listener found. Verify Oracle is running on host/port (default: 1521)."
    if "ora-12514" in message or "listener does not currently know of service" in message:
        return "DATABASE_ACCESS: TNS listener does not recognize the service name. Verify service_name in connection config."
    if "ora-12505" in message or "listener could not resolve sid" in message:
        return "DATABASE_ACCESS: TNS listener could not resolve SID. Verify SID in connection config."
    if "ora-12170" in message or "tns:connect timeout occurred" in message:
        return "NETWORK_UNREACHABLE: Oracle connection timeout expired. Verify host, port, and network reachability."
    if "ora-00942" in message or "table or view does not exist" in message:
        return "PERMISSION_ERROR: Oracle table/view does not exist or user lacks SELECT privileges on data dictionary."
    if "dpyp-4011" in message or "dpyp-4000" in message:
        return "AUTHENTICATION_FAILED: Oracle connection parameters error. Verify host, port, service name / SID, and user credentials."
    # MySQL specific diagnostics
    if "1045" in message or "access denied for user" in message:
        return "AUTHENTICATION_FAILED: Verify MySQL username and password."
    if "1049" in message or "unknown database" in message:
        return "DATABASE_ACCESS: Verify the MySQL database name exists and user has access."
    if "2003" in message or "can't connect to mysql server" in message or "connection refused" in message:
        return "NETWORK_UNREACHABLE: The backend could not reach MySQL server. Verify MySQL is running on host/port (default: 3306)."
    if "1044" in message:
        return "DATABASE_ACCESS: Access denied for database. Check user database permissions."
    # PostgreSQL specific diagnostics
    if "password authentication failed" in message or "28p01" in message:
        return "AUTHENTICATION_FAILED: Verify PostgreSQL username and password."
    if "database" in message and ("does not exist" in message or "3d000" in message):
        return "DATABASE_ACCESS: Verify the PostgreSQL database name exists and is accessible."
    if "could not connect to server" in message or "08001" in message:
        return "NETWORK_UNREACHABLE: The backend could not reach PostgreSQL host/port. Verify network, host, and port 5432."
    if "ssl" in message or "certificate" in message:
        return "TLS_ERROR: Verify SSL configuration and sslmode setting."
    if "hyt00" in message or "timeout" in message:
        return "NETWORK_UNREACHABLE: Connection timeout expired. Verify server host and port."
    if "4060" in message or "cannot open database" in message:
        return "DATABASE_ACCESS: Verify the database name and grant connector read and metadata permissions."
    if "28000" in message or "login failed" in message or "18456" in message:
        return "AUTHENTICATION_FAILED: Verify database credentials."
    if "im002" in message or "driver" in message and ("not found" in message or "can't open" in message):
        return "DRIVER_MISSING: Install required driver."
    return f"SOURCE_OPERATION_FAILED: {str(error)}"


# ==============================================================================
# ORACLE DISCOVERY IMPLEMENTATION
# ==============================================================================

def parse_oracle_conn(conn_info: Any) -> dict:
    if isinstance(conn_info, dict):
        cfg = dict(conn_info)
        if "username" in cfg and "user" not in cfg:
            cfg["user"] = cfg.pop("username")
        if "dbname" in cfg and "service_name" not in cfg and "sid" not in cfg:
            cfg["service_name"] = cfg.pop("dbname")
        if "database" in cfg and "service_name" not in cfg and "sid" not in cfg:
            cfg["service_name"] = cfg.pop("database")
        if "port" in cfg:
            try:
                cfg["port"] = int(cfg["port"])
            except (ValueError, TypeError):
                cfg["port"] = 1521
        else:
            cfg["port"] = 1521
        return cfg
    s = str(conn_info).strip()
    if s.startswith("oracle://") or s.startswith("oracle+oracledb://") or s.startswith("oracle+cx_oracle://"):
        u = urlparse(s)
        q = parse_qs(u.query)
        sid = q.get("sid", [None])[0]
        service_name = q.get("service_name", [None])[0]
        schema = q.get("schema", [None])[0]
        path_clean = u.path.lstrip("/") if u.path else ""
        if not service_name and not sid and path_clean:
            service_name = path_clean
        return {
            "host": u.hostname or "localhost",
            "port": int(u.port or 1521),
            "service_name": service_name,
            "sid": sid,
            "schema": schema,
            "user": u.username or "system",
            "password": u.password or "",
        }
    parts = s.split()
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
    if "port" in out:
        try:
            out["port"] = int(out["port"])
        except (ValueError, TypeError):
            out["port"] = 1521
    else:
        out["port"] = 1521
    if "username" in out and "user" not in out:
        out["user"] = out.pop("username")
    if "dbname" in out and "service_name" not in out and "sid" not in out:
        out["service_name"] = out.pop("dbname")
    if "database" in out and "service_name" not in out and "sid" not in out:
        out["service_name"] = out.pop("database")
    return out


def _get_oracle_connection(cfg: dict):
    try:
        import oracledb
    except ImportError as e:
        raise RuntimeError("python-oracledb is required for Oracle connectivity. Install with: pip install oracledb") from e

    user = cfg.get("user") or cfg.get("username") or "system"
    password = cfg.get("password") or ""
    host = cfg.get("host") or "localhost"
    port = int(cfg.get("port", 1521))
    service_name = cfg.get("service_name")
    sid = cfg.get("sid")
    dsn = cfg.get("dsn")

    if dsn:
        return oracledb.connect(user=user, password=password, dsn=dsn)

    target_name = service_name or sid or "ORCLPDB1"

    # Try connecting via Service Name first
    if service_name or not sid:
        dsn_service = f"{host}:{port}/{target_name}"
        try:
            return oracledb.connect(user=user, password=password, dsn=dsn_service)
        except Exception as e:
            err_msg = str(e).lower()
            # If listener doesn't know service name, try SID fallback
            if "ora-12514" in err_msg or "listener does not currently know of service" in err_msg:
                try:
                    dsn_sid = oracledb.makedsn(host, port, sid=target_name)
                    return oracledb.connect(user=user, password=password, dsn=dsn_sid)
                except Exception:
                    pass
            raise e
    else:
        # Try connecting via SID first
        dsn_sid = oracledb.makedsn(host, port, sid=target_name)
        try:
            return oracledb.connect(user=user, password=password, dsn=dsn_sid)
        except Exception as e:
            err_msg = str(e).lower()
            # If listener can't resolve SID, try Service Name fallback
            if "ora-12505" in err_msg or "listener could not resolve sid" in err_msg:
                try:
                    dsn_service = f"{host}:{port}/{target_name}"
                    return oracledb.connect(user=user, password=password, dsn=dsn_service)
                except Exception:
                    pass
            raise e


def test_oracle_connection(conn_info: Any) -> dict[str, Any]:
    cfg = parse_oracle_conn(conn_info)
    try:
        conn = _get_oracle_connection(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT SYS_CONTEXT('USERENV', 'DB_NAME'), SYS_CONTEXT('USERENV', 'SERVER_HOST'), SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
                row = cur.fetchone()
                db_name = row[0] if row and row[0] else (cfg.get("service_name") or cfg.get("sid") or "")
                server_addr = row[1] if row and row[1] else cfg.get("host", "localhost")
                schema_name = row[2] if row and row[2] else ""
                version = f"Oracle {conn.version}" if hasattr(conn, 'version') else "Oracle Database"
                return {
                    "ok": True,
                    "server": server_addr or "localhost",
                    "database": db_name or schema_name or "Oracle",
                    "schema": schema_name,
                    "product_version": version,
                }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


def discover_oracle(conn_info: Any) -> dict[str, Any]:
    cfg = parse_oracle_conn(conn_info)
    target_schema = (cfg.get("schema") or "").upper()
    try:
        conn = _get_oracle_connection(cfg)
        try:
            with conn.cursor() as cur:
                excluded_schemas = (
                    "'SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'APPQOSSYS', 'CTXSYS', "
                    "'XDB', 'WMSYS', 'MDSYS', 'ORDSYS', 'ORDDATA', 'OJVMSYS', "
                    "'LBACSYS', 'GSMADMIN_INTERNAL', 'AUDSYS', 'ANONYMOUS', "
                    "'DIP', 'FLOWS_FILES', 'GSMUSER', 'MGMT_VIEW', 'ORACLE_OCM', "
                    "'OWBSYS', 'SI_INFORMTN_SCHEMA', 'SPATIAL_CSW_ADMIN_USR', "
                    "'SPATIAL_WFS_ADMIN_USR', 'XS$NULL', 'DVSYS', 'DVF', "
                    "'REMOTE_SCHEDULER_AGENT', 'ORDPLUGINS', 'PUBLIC', "
                    "'OLAPSYS', 'VECSYS', 'DBSFWUSER', 'GGSHAREDCAP', 'GGSYS', "
                    "'SYS$UMF', 'APEX_PUBLIC_USER', 'ORDS_PUBLIC_USER', 'ORDS_METADATA'"
                )

                if target_schema:
                    owner_where = "UPPER(OWNER) = :owner"
                    c_owner_where = "UPPER(c.OWNER) = :owner"
                    owner_args = {"owner": target_schema}
                else:
                    owner_where = f"OWNER NOT IN ({excluded_schemas})"
                    c_owner_where = f"c.OWNER NOT IN ({excluded_schemas})"
                    owner_args = {}

                def _fetch_objs(where_clause, args):
                    cur.execute(f"""
                        SELECT 
                            SYS_CONTEXT('USERENV', 'DB_NAME') AS database_name,
                            OWNER AS schema_name,
                            TABLE_NAME AS object_name,
                            'TABLE' AS object_type,
                            NULL AS definition
                        FROM ALL_TABLES
                        WHERE {where_clause}
                        UNION ALL
                        SELECT 
                            SYS_CONTEXT('USERENV', 'DB_NAME') AS database_name,
                            OWNER AS schema_name,
                            VIEW_NAME AS object_name,
                            'VIEW' AS object_type,
                            TEXT AS definition
                        FROM ALL_VIEWS
                        WHERE {where_clause}
                        UNION ALL
                        SELECT 
                            SYS_CONTEXT('USERENV', 'DB_NAME') AS database_name,
                            OWNER AS schema_name,
                            OBJECT_NAME AS object_name,
                            OBJECT_TYPE AS object_type,
                            NULL AS definition
                        FROM ALL_PROCEDURES
                        WHERE {where_clause}
                          AND OBJECT_TYPE IN ('PROCEDURE', 'FUNCTION', 'PACKAGE')
                          AND PROCEDURE_NAME IS NULL
                        UNION ALL
                        SELECT 
                            SYS_CONTEXT('USERENV', 'DB_NAME') AS database_name,
                            OWNER AS schema_name,
                            TRIGGER_NAME AS object_name,
                            'TRIGGER' AS object_type,
                            TRIGGER_BODY AS definition
                        FROM ALL_TRIGGERS
                        WHERE {where_clause}
                    """, args)
                    return cur.fetchall()

                obj_rows = _fetch_objs(owner_where, owner_args)
                # If target schema returned 0 objects, discover all non-system user schemas
                if not obj_rows and target_schema:
                    owner_where = f"OWNER NOT IN ({excluded_schemas})"
                    c_owner_where = f"c.OWNER NOT IN ({excluded_schemas})"
                    owner_args = {}
                    obj_rows = _fetch_objs(owner_where, owner_args)
                # Fetch routine/procedure definitions from ALL_SOURCE
                source_defs: dict[tuple[str, str], list[str]] = {}
                try:
                    cur.execute(f"""
                        SELECT OWNER, NAME, TYPE, LINE, TEXT
                        FROM ALL_SOURCE
                        WHERE {owner_where}
                        ORDER BY OWNER, NAME, TYPE, LINE
                    """, owner_args)
                    for s_owner, s_name, s_type, s_line, s_text in cur.fetchall():
                        source_defs.setdefault((s_owner, s_name), []).append(s_text or "")
                except Exception:
                    pass

                objs = []
                for r in obj_rows:
                    schema_name = r[1]
                    object_name = r[2]
                    object_type = r[3]
                    definition = str(r[4]) if r[4] is not None else None
                    if (not definition or definition.strip() == "") and (schema_name, object_name) in source_defs:
                        full_source = "".join(source_defs[(schema_name, object_name)])
                        if full_source.strip():
                            trimmed = full_source.strip()
                            if not trimmed.upper().startswith("CREATE"):
                                full_source = f"CREATE OR REPLACE {trimmed}"
                            definition = full_source
                    objs.append({
                        "database_name": r[0] or target_schema or "Oracle",
                        "schema_name": schema_name,
                        "object_name": object_name,
                        "object_type": object_type,
                        "definition": definition
                    })

                # 2. Columns
                cur.execute(f"""
                    SELECT 
                        OWNER AS schema_name,
                        TABLE_NAME AS object_name,
                        COLUMN_NAME AS column_name,
                        COLUMN_ID AS column_id,
                        DATA_TYPE || 
                            CASE 
                                WHEN DATA_TYPE IN ('VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR') THEN '(' || DATA_LENGTH || ')'
                                WHEN DATA_TYPE = 'NUMBER' AND DATA_PRECISION IS NOT NULL AND NVL(DATA_SCALE, 0) > 0 THEN '(' || DATA_PRECISION || ',' || DATA_SCALE || ')'
                                WHEN DATA_TYPE = 'NUMBER' AND DATA_PRECISION IS NOT NULL THEN '(' || DATA_PRECISION || ')'
                                ELSE ''
                            END AS declared_data_type,
                        DATA_TYPE AS system_data_type,
                        0 AS is_user_defined,
                        CHAR_LENGTH AS max_length,
                        DATA_PRECISION AS precision_val,
                        DATA_SCALE AS scale_val,
                        CASE WHEN NULLABLE = 'Y' THEN 1 ELSE 0 END AS is_nullable,
                        0 AS is_identity,
                        0 AS is_computed,
                        NULL AS default_definition,
                        CHARACTER_SET_NAME AS collation_name
                    FROM ALL_TAB_COLUMNS
                    WHERE {owner_where}
                    ORDER BY OWNER, TABLE_NAME, COLUMN_ID
                """, owner_args)
                col_rows = cur.fetchall()
                cols = [
                    {
                        "schema_name": r[0], "object_name": r[1], "column_name": r[2],
                        "column_id": r[3] or 0, "declared_data_type": r[4], "system_data_type": r[5],
                        "is_user_defined": bool(r[6]), "max_length": r[7], "precision": r[8],
                        "scale": r[9], "is_nullable": bool(r[10]), "is_identity": bool(r[11]),
                        "is_computed": bool(r[12]), "default_definition": r[13], "collation_name": r[14]
                    }
                    for r in col_rows
                ]

                # 3. Key Constraints (Primary Key & Unique)
                try:
                    cur.execute(f"""
                        SELECT 
                            c.OWNER AS schema_name,
                            c.TABLE_NAME AS object_name,
                            c.CONSTRAINT_NAME AS constraint_name,
                            CASE c.CONSTRAINT_TYPE 
                                WHEN 'P' THEN 'PRIMARY KEY'
                                WHEN 'U' THEN 'UNIQUE'
                                ELSE c.CONSTRAINT_TYPE 
                            END AS constraint_type,
                            cc.POSITION AS key_ordinal,
                            cc.COLUMN_NAME AS column_name
                        FROM ALL_CONSTRAINTS c
                        JOIN ALL_CONS_COLUMNS cc 
                          ON c.OWNER = cc.OWNER 
                          AND c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME 
                          AND c.TABLE_NAME = cc.TABLE_NAME
                        WHERE c.CONSTRAINT_TYPE IN ('P', 'U')
                          AND {c_owner_where}
                        ORDER BY c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, cc.POSITION
                    """, owner_args)
                    kc_rows = cur.fetchall()
                    key_constraints = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "constraint_type": r[3], "key_ordinal": r[4], "column_name": r[5]
                        }
                        for r in kc_rows
                    ]
                except Exception:
                    key_constraints = []

                # 4. Foreign Keys
                try:
                    cur.execute(f"""
                        SELECT 
                            c.OWNER AS schema_name,
                            c.TABLE_NAME AS object_name,
                            c.CONSTRAINT_NAME AS constraint_name,
                            cc.POSITION AS ordinal,
                            cc.COLUMN_NAME AS column_name,
                            r_c.OWNER AS referenced_schema,
                            r_c.TABLE_NAME AS referenced_object,
                            r_cc.COLUMN_NAME AS referenced_column
                        FROM ALL_CONSTRAINTS c
                        JOIN ALL_CONS_COLUMNS cc 
                          ON c.OWNER = cc.OWNER 
                          AND c.CONSTRAINT_NAME = cc.CONSTRAINT_NAME 
                          AND c.TABLE_NAME = cc.TABLE_NAME
                        JOIN ALL_CONSTRAINTS r_c 
                          ON c.R_OWNER = r_c.OWNER 
                          AND c.R_CONSTRAINT_NAME = r_c.CONSTRAINT_NAME
                        JOIN ALL_CONS_COLUMNS r_cc 
                          ON r_c.OWNER = r_cc.OWNER 
                          AND r_c.CONSTRAINT_NAME = r_cc.CONSTRAINT_NAME 
                          AND cc.POSITION = r_cc.POSITION
                        WHERE c.CONSTRAINT_TYPE = 'R'
                          AND {c_owner_where}
                        ORDER BY c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, cc.POSITION
                    """, owner_args)
                    fk_rows = cur.fetchall()
                    foreign_keys = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "ordinal": r[3], "column_name": r[4], "referenced_schema": r[5],
                            "referenced_object": r[6], "referenced_column": r[7]
                        }
                        for r in fk_rows
                    ]
                except Exception:
                    foreign_keys = []

                # 5. Table Statistics
                try:
                    cur.execute(f"""
                        SELECT 
                            OWNER AS schema_name,
                            TABLE_NAME AS object_name,
                            NVL(NUM_ROWS, 0) AS approx_row_count
                        FROM ALL_TABLES
                        WHERE {owner_where}
                    """, owner_args)
                    stat_rows = cur.fetchall()
                    table_stats = [
                        {"schema_name": r[0], "object_name": r[1], "approx_row_count": r[2] or 0}
                        for r in stat_rows
                    ]
                except Exception:
                    table_stats = []

                # 6. Dependencies
                try:
                    cur.execute(f"""
                        SELECT 
                            OWNER AS referencing_schema_name,
                            NAME AS referencing_entity_name,
                            REFERENCED_OWNER AS referenced_schema_name,
                            REFERENCED_NAME AS referenced_entity_name,
                            REFERENCED_TYPE AS referenced_type,
                            'LOCAL' AS dependency_scope
                        FROM ALL_DEPENDENCIES
                        WHERE {owner_where}
                    """, owner_args)
                    dep_rows = cur.fetchall()
                    deps = [
                        {
                            "referencing_schema_name": r[0],
                            "referencing_entity_name": r[1],
                            "referenced_schema_name": r[2],
                            "referenced_entity_name": r[3],
                            "referenced_column_name": None,
                            "referenced_minor_id": None,
                            "dependency_scope": r[5],
                            "is_schema_bound_reference": False,
                            "is_caller_dependent": False,
                            "is_ambiguous": False,
                        }
                        for r in dep_rows
                    ]
                except Exception:
                    deps = []

            by: dict[tuple[str, str], list[dict[str, Any]]] = {(r["schema_name"], r["object_name"]): [] for r in objs}
            for c in cols:
                by.setdefault((c["schema_name"], c["object_name"]), []).append({
                    "name": c["column_name"],
                    "ordinal": c["column_id"],
                    "type": c["declared_data_type"] or c["system_data_type"],
                    "declared_type": c["declared_data_type"],
                    "system_type": c["system_data_type"],
                    "is_user_defined": c["is_user_defined"],
                    "max_length": c["max_length"],
                    "precision": c["precision"],
                    "scale": c["scale"],
                    "nullable": c["is_nullable"],
                    "identity": c["is_identity"],
                    "computed": c["is_computed"],
                    "default": c["default_definition"],
                    "collation": c["collation_name"]
                })

            dep_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for d in deps:
                dep_by.setdefault((d["referencing_schema_name"], d["referencing_entity_name"]), []).append({
                    "server": None,
                    "database": cfg.get("service_name") or cfg.get("sid") or target_schema,
                    "schema": d["referenced_schema_name"],
                    "object": d["referenced_entity_name"],
                    "column": d["referenced_column_name"],
                    "referenced_minor_id": d["referenced_minor_id"],
                    "type": d["dependency_scope"],
                    "is_schema_bound_reference": d["is_schema_bound_reference"],
                    "is_caller_dependent": d["is_caller_dependent"],
                    "is_ambiguous": d["is_ambiguous"]
                })

            constraint_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            grouped_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
            for k in key_constraints:
                key = (k["schema_name"], k["object_name"], k["constraint_name"])
                row = grouped_keys.setdefault(key, {
                    "name": k["constraint_name"],
                    "type": "PRIMARY_KEY" if str(k["constraint_type"]).upper().startswith("PRIMARY") else "UNIQUE",
                    "columns": [],
                })
                row["columns"].append(k["column_name"])
            for (sch, obj, _), row in grouped_keys.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            grouped_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
            for f in foreign_keys:
                key = (f["schema_name"], f["object_name"], f["constraint_name"])
                row = grouped_fks.setdefault(key, {
                    "name": f["constraint_name"],
                    "type": "FOREIGN_KEY",
                    "columns": [],
                    "referenced_schema": f["referenced_schema"],
                    "referenced_object": f["referenced_object"],
                    "referenced_columns": [],
                })
                row["columns"].append(f["column_name"])
                row["referenced_columns"].append(f["referenced_column"])
            for (sch, obj, _), row in grouped_fks.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            stats_by = {(r["schema_name"], r["object_name"]): int(r["approx_row_count"] or 0) for r in table_stats}

            return {
                "database": cfg.get("service_name") or cfg.get("sid") or (objs[0]["database_name"] if objs else target_schema),
                "objects": [{
                    "database": r["database_name"],
                    "schema": r["schema_name"],
                    "name": r["object_name"],
                    "type": r["object_type"],
                    "definition": r["definition"],
                    "columns": by.get((r["schema_name"], r["object_name"]), []),
                    "dependencies": dep_by.get((r["schema_name"], r["object_name"]), []),
                    "parameters": [],
                    "constraints": constraint_by.get((r["schema_name"], r["object_name"]), []),
                    "approx_row_count": stats_by.get((r["schema_name"], r["object_name"]))
                } for r in objs]
            }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


# ==============================================================================
# MYSQL DISCOVERY IMPLEMENTATION
# ==============================================================================

def parse_mysql_conn(conn_info: Any) -> dict:
    if isinstance(conn_info, dict):
        cfg = dict(conn_info)
        if "dbname" in cfg and "database" not in cfg:
            cfg["database"] = cfg.pop("dbname")
        if "username" in cfg and "user" not in cfg:
            cfg["user"] = cfg.pop("username")
        if "port" in cfg:
            try:
                cfg["port"] = int(cfg["port"])
            except (ValueError, TypeError):
                cfg["port"] = 3306
        return cfg
    s = str(conn_info).strip()
    if s.startswith("mysql://") or s.startswith("mysql+pymysql://") or s.startswith("mysql+mysqlconnector://"):
        u = urlparse(s)
        return {
            "host": u.hostname or "localhost",
            "port": int(u.port or 3306),
            "database": u.path.lstrip("/") if u.path else "",
            "user": u.username or "root",
            "password": u.password or "",
        }
    parts = s.split()
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    if "port" in out:
        try:
            out["port"] = int(out["port"])
        except (ValueError, TypeError):
            out["port"] = 3306
    if "username" in out and "user" not in out:
        out["user"] = out.pop("username")
    if "dbname" in out and "database" not in out:
        out["database"] = out.pop("dbname")
    return out


def _get_mysql_connection(cfg: dict):
    # Try PyMySQL first, then mysql.connector
    try:
        import pymysql
        conn_kwargs = {
            "host": cfg.get("host", "localhost"),
            "port": int(cfg.get("port", 3306)),
            "user": cfg.get("user", "root"),
            "password": cfg.get("password", ""),
            "database": cfg.get("database") or None,
            "charset": "utf8mb4",
            "connect_timeout": 10,
        }
        return pymysql.connect(**conn_kwargs)
    except ImportError:
        try:
            import mysql.connector
            conn_kwargs = {
                "host": cfg.get("host", "localhost"),
                "port": int(cfg.get("port", 3306)),
                "user": cfg.get("user", "root"),
                "password": cfg.get("password", ""),
                "database": cfg.get("database") or None,
                "connection_timeout": 10,
            }
            return mysql.connector.connect(**conn_kwargs)
        except ImportError as e:
            raise RuntimeError("PyMySQL or mysql-connector-python is required for MySQL connectivity") from e


def test_mysql_connection(conn_info: Any) -> dict[str, Any]:
    cfg = parse_mysql_conn(conn_info)
    try:
        conn = _get_mysql_connection(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE(), @@hostname, VERSION();")
                row = cur.fetchone()
                db_name = row[0] if row and row[0] else cfg.get("database", "")
                server_addr = row[1] if row and row[1] else cfg.get("host", "localhost")
                version = f"MySQL {row[2]}" if row and row[2] else "MySQL"
                return {
                    "ok": True,
                    "server": server_addr or "localhost",
                    "database": db_name or "",
                    "product_version": version,
                }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


def discover_mysql(conn_info: Any) -> dict[str, Any]:
    cfg = parse_mysql_conn(conn_info)
    db_target = cfg.get("database") or ""
    try:
        conn = _get_mysql_connection(cfg)
        try:
            with conn.cursor() as cur:
                if not db_target:
                    cur.execute("SELECT DATABASE();")
                    row = cur.fetchone()
                    db_target = row[0] if row and row[0] else ""

                schema_filter = "TABLE_SCHEMA = %s" if db_target else "TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
                schema_args = [db_target] if db_target else []

                # 1. Objects: Tables & Views
                tbl_query = f"""
                SELECT
                    TABLE_SCHEMA AS database_name,
                    TABLE_SCHEMA AS schema_name,
                    TABLE_NAME AS object_name,
                    CASE TABLE_TYPE
                        WHEN 'VIEW' THEN 'VIEW'
                        ELSE 'TABLE'
                    END AS object_type,
                    VIEW_DEFINITION AS definition
                FROM information_schema.TABLES
                LEFT JOIN information_schema.VIEWS USING (TABLE_SCHEMA, TABLE_NAME)
                WHERE {schema_filter};
                """
                cur.execute(tbl_query, schema_args)
                obj_rows = cur.fetchall()

                # Stored Routines (Procedures and Functions)
                routine_filter = "ROUTINE_SCHEMA = %s" if db_target else "ROUTINE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
                routine_query = f"""
                SELECT
                    ROUTINE_SCHEMA AS database_name,
                    ROUTINE_SCHEMA AS schema_name,
                    ROUTINE_NAME AS object_name,
                    ROUTINE_TYPE AS object_type,
                    ROUTINE_DEFINITION AS definition
                FROM information_schema.ROUTINES
                WHERE {routine_filter};
                """
                try:
                    cur.execute(routine_query, schema_args)
                    routine_rows = cur.fetchall()
                except Exception:
                    routine_rows = []

                # Triggers
                trigger_filter = "TRIGGER_SCHEMA = %s" if db_target else "TRIGGER_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
                trigger_query = f"""
                SELECT
                    TRIGGER_SCHEMA AS database_name,
                    TRIGGER_SCHEMA AS schema_name,
                    TRIGGER_NAME AS object_name,
                    'TRIGGER' AS object_type,
                    ACTION_STATEMENT AS definition
                FROM information_schema.TRIGGERS
                WHERE {trigger_filter};
                """
                try:
                    cur.execute(trigger_query, schema_args)
                    trigger_rows = cur.fetchall()
                except Exception:
                    trigger_rows = []

                all_objs = list(obj_rows) + list(routine_rows) + list(trigger_rows)
                objs = [
                    {
                        "database_name": r[0], "schema_name": r[1],
                        "object_name": r[2], "object_type": r[3], "definition": r[4]
                    }
                    for r in all_objs
                ]

                # 2. Columns
                col_query = f"""
                SELECT
                    TABLE_SCHEMA AS schema_name,
                    TABLE_NAME AS object_name,
                    COLUMN_NAME AS column_name,
                    ORDINAL_POSITION AS column_id,
                    COLUMN_TYPE AS declared_data_type,
                    DATA_TYPE AS system_data_type,
                    0 AS is_user_defined,
                    CHARACTER_MAXIMUM_LENGTH AS max_length,
                    NUMERIC_PRECISION AS precision_val,
                    NUMERIC_SCALE AS scale_val,
                    CASE WHEN IS_NULLABLE = 'YES' THEN 1 ELSE 0 END AS is_nullable,
                    CASE WHEN EXTRA LIKE '%%auto_increment%%' THEN 1 ELSE 0 END AS is_identity,
                    CASE WHEN EXTRA LIKE '%%VIRTUAL%%' OR EXTRA LIKE '%%STORED%%' THEN 1 ELSE 0 END AS is_computed,
                    COLUMN_DEFAULT AS default_definition,
                    COLLATION_NAME AS collation_name
                FROM information_schema.COLUMNS
                WHERE {schema_filter}
                ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
                """
                cur.execute(col_query, schema_args)
                col_rows = cur.fetchall()
                cols = [
                    {
                        "schema_name": r[0], "object_name": r[1], "column_name": r[2],
                        "column_id": r[3], "declared_data_type": r[4], "system_data_type": r[5],
                        "is_user_defined": bool(r[6]), "max_length": r[7], "precision": r[8],
                        "scale": r[9], "is_nullable": bool(r[10]), "is_identity": bool(r[11]),
                        "is_computed": bool(r[12]), "default_definition": r[13], "collation_name": r[14]
                    }
                    for r in col_rows
                ]

                # 3. Key Constraints (Primary Key & Unique)
                kc_filter = "tc.TABLE_SCHEMA = %s" if db_target else "tc.TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
                kc_query = f"""
                SELECT
                    tc.TABLE_SCHEMA AS schema_name,
                    tc.TABLE_NAME AS object_name,
                    tc.CONSTRAINT_NAME AS constraint_name,
                    tc.CONSTRAINT_TYPE AS constraint_type,
                    kcu.ORDINAL_POSITION AS key_ordinal,
                    kcu.COLUMN_NAME AS column_name
                FROM information_schema.TABLE_CONSTRAINTS tc
                JOIN information_schema.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                 AND tc.TABLE_NAME = kcu.TABLE_NAME
                WHERE {kc_filter}
                  AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE')
                ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;
                """
                try:
                    cur.execute(kc_query, schema_args)
                    kc_rows = cur.fetchall()
                    key_constraints = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "constraint_type": r[3], "key_ordinal": r[4], "column_name": r[5]
                        }
                        for r in kc_rows
                    ]
                except Exception:
                    key_constraints = []

                # 4. Foreign Keys
                fk_filter = "kcu.TABLE_SCHEMA = %s" if db_target else "kcu.TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')"
                fk_query = f"""
                SELECT
                    kcu.TABLE_SCHEMA AS schema_name,
                    kcu.TABLE_NAME AS object_name,
                    kcu.CONSTRAINT_NAME AS constraint_name,
                    kcu.ORDINAL_POSITION AS ordinal,
                    kcu.COLUMN_NAME AS column_name,
                    kcu.REFERENCED_TABLE_SCHEMA AS referenced_schema,
                    kcu.REFERENCED_TABLE_NAME AS referenced_object,
                    kcu.REFERENCED_COLUMN_NAME AS referenced_column
                FROM information_schema.KEY_COLUMN_USAGE kcu
                JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                  ON rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                 AND rc.CONSTRAINT_SCHEMA = kcu.TABLE_SCHEMA
                WHERE {fk_filter}
                  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY kcu.TABLE_SCHEMA, kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;
                """
                try:
                    cur.execute(fk_query, schema_args)
                    fk_rows = cur.fetchall()
                    foreign_keys = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "ordinal": r[3], "column_name": r[4], "referenced_schema": r[5],
                            "referenced_object": r[6], "referenced_column": r[7]
                        }
                        for r in fk_rows
                    ]
                except Exception:
                    foreign_keys = []

                # 5. Table Statistics
                stat_query = f"""
                SELECT
                    TABLE_SCHEMA AS schema_name,
                    TABLE_NAME AS object_name,
                    TABLE_ROWS AS approx_row_count
                FROM information_schema.TABLES
                WHERE {schema_filter} AND TABLE_TYPE = 'BASE TABLE';
                """
                try:
                    cur.execute(stat_query, schema_args)
                    stat_rows = cur.fetchall()
                    table_stats = [
                        {"schema_name": r[0], "object_name": r[1], "approx_row_count": r[2] or 0}
                        for r in stat_rows
                    ]
                except Exception:
                    table_stats = []

            by: dict[tuple[str, str], list[dict[str, Any]]] = {(r["schema_name"], r["object_name"]): [] for r in objs}
            for c in cols:
                by.setdefault((c["schema_name"], c["object_name"]), []).append({
                    "name": c["column_name"],
                    "ordinal": c["column_id"],
                    "type": c["declared_data_type"] or c["system_data_type"],
                    "declared_type": c["declared_data_type"],
                    "system_type": c["system_data_type"],
                    "is_user_defined": bool(c["is_user_defined"]),
                    "max_length": c["max_length"],
                    "precision": c["precision"],
                    "scale": c["scale"],
                    "nullable": bool(c["is_nullable"]),
                    "identity": bool(c["is_identity"]),
                    "computed": bool(c["is_computed"]),
                    "default": c["default_definition"],
                    "collation": c["collation_name"],
                })

            constraint_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            grouped_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
            for k in key_constraints:
                key = (k["schema_name"], k["object_name"], k["constraint_name"])
                row = grouped_keys.setdefault(key, {
                    "name": k["constraint_name"],
                    "type": "PRIMARY_KEY" if str(k["constraint_type"]).upper().startswith("PRIMARY") else "UNIQUE",
                    "columns": [],
                })
                row["columns"].append(k["column_name"])
            for (sch, obj, _), row in grouped_keys.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            grouped_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
            for f in foreign_keys:
                key = (f["schema_name"], f["object_name"], f["constraint_name"])
                row = grouped_fks.setdefault(key, {
                    "name": f["constraint_name"], "type": "FOREIGN_KEY", "columns": [],
                    "referenced_schema": f["referenced_schema"], "referenced_object": f["referenced_object"],
                    "referenced_columns": [],
                })
                row["columns"].append(f["column_name"])
                row["referenced_columns"].append(f["referenced_column"])
            for (sch, obj, _), row in grouped_fks.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            stats_by = {(r["schema_name"], r["object_name"]): int(r["approx_row_count"] or 0) for r in table_stats}

            # 6. Dependencies from Foreign Keys and Views
            dep_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for f in foreign_keys:
                dep_by.setdefault((f["schema_name"], f["object_name"]), []).append({
                    "server": None,
                    "database": f["referenced_schema"],
                    "schema": f["referenced_schema"],
                    "object": f["referenced_object"],
                    "column": f["referenced_column"],
                    "referenced_minor_id": None,
                    "type": "FOREIGN_KEY",
                    "is_schema_bound_reference": True,
                    "is_caller_dependent": False,
                    "is_ambiguous": False,
                })

            view_dep_query = f"""
            SELECT
                VIEW_SCHEMA AS referencing_schema_name,
                VIEW_NAME AS referencing_entity_name,
                TABLE_SCHEMA AS referenced_schema_name,
                TABLE_NAME AS referenced_entity_name
            FROM information_schema.VIEW_TABLE_USAGE
            WHERE {schema_filter};
            """
            try:
                cur.execute(view_dep_query, schema_args)
                view_dep_rows = cur.fetchall()
                for v in view_dep_rows:
                    dep_by.setdefault((v[0], v[1]), []).append({
                        "server": None,
                        "database": v[2],
                        "schema": v[2],
                        "object": v[3],
                        "column": None,
                        "referenced_minor_id": None,
                        "type": "VIEW_USAGE",
                        "is_schema_bound_reference": True,
                        "is_caller_dependent": False,
                        "is_ambiguous": False,
                    })
            except Exception:
                pass

            # Also parse view/routine definitions for table references
            table_names_set = {r["object_name"].lower() for r in objs if r["object_type"] == "TABLE"}
            for r in objs:
                if r["object_type"] in {"VIEW", "PROCEDURE", "FUNCTION"} and r.get("definition"):
                    defn = (r["definition"] or "").lower()
                    existing_refs = {d["object"].lower() for d in dep_by.get((r["schema_name"], r["object_name"]), [])}
                    for tname in table_names_set:
                        if tname != r["object_name"].lower() and tname not in existing_refs and re.search(r'\b' + re.escape(tname) + r'\b', defn):
                            dep_by.setdefault((r["schema_name"], r["object_name"]), []).append({
                                "server": None,
                                "database": r["database_name"],
                                "schema": r["schema_name"],
                                "object": tname,
                                "column": None,
                                "referenced_minor_id": None,
                                "type": "SQL_REFERENCE",
                                "is_schema_bound_reference": True,
                                "is_caller_dependent": False,
                                "is_ambiguous": False,
                            })

            return {
                "database": db_target or (objs[0]["database_name"] if objs else ""),
                "objects": [{
                    "database": r["database_name"],
                    "schema": r["schema_name"],
                    "name": r["object_name"],
                    "type": r["object_type"],
                    "definition": r["definition"],
                    "columns": by.get((r["schema_name"], r["object_name"]), []),
                    "dependencies": dep_by.get((r["schema_name"], r["object_name"]), []),
                    "parameters": [],
                    "constraints": constraint_by.get((r["schema_name"], r["object_name"]), []),
                    "approx_row_count": stats_by.get((r["schema_name"], r["object_name"]))
                } for r in objs]
            }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


# ==============================================================================
# POSTGRESQL DISCOVERY IMPLEMENTATION
# ==============================================================================

def parse_postgres_conn(conn_info: Any) -> dict:
    if isinstance(conn_info, dict):
        return conn_info
    s = str(conn_info).strip()
    if s.startswith("postgresql://") or s.startswith("postgres://"):
        u = urlparse(s)
        params = parse_qs(u.query)
        return {
            "host": u.hostname or "localhost",
            "port": u.port or 5432,
            "dbname": u.path.lstrip("/") if u.path else "postgres",
            "user": u.username or "postgres",
            "password": u.password or "",
            "sslmode": params.get("sslmode", ["prefer"])[0],
        }
    parts = s.split()
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_postgres_connection(conn_info: Any) -> dict[str, Any]:
    try:
        import psycopg2
    except ImportError:
        try:
            import psycopg as psycopg2
        except ImportError as e:
            raise RuntimeError("psycopg2-binary or psycopg is required for PostgreSQL connectivity") from e
    
    cfg = parse_postgres_conn(conn_info)
    try:
        conn = psycopg2.connect(**cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), inet_server_addr()::text, version()")
                row = cur.fetchone()
                db_name = row[0] if row else cfg.get("dbname", "postgres")
                server_addr = row[1] if row and row[1] else cfg.get("host", "localhost")
                version = row[2] if row else "PostgreSQL"
                return {
                    "ok": True,
                    "server": server_addr or "localhost",
                    "database": db_name,
                    "product_version": version,
                }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


def discover_postgres(conn_info: Any) -> dict[str, Any]:
    try:
        import psycopg2
    except ImportError:
        try:
            import psycopg as psycopg2
        except ImportError as e:
            raise RuntimeError("psycopg2-binary or psycopg is required for PostgreSQL discovery") from e
    
    cfg = parse_postgres_conn(conn_info)
    try:
        conn = psycopg2.connect(**cfg)
        try:
            with conn.cursor() as cur:
                # 1. Objects
                cur.execute(PG_DISCOVERY_SQL)
                obj_rows = cur.fetchall()
                objs = [
                    {
                        "database_name": r[0], "schema_name": r[1],
                        "object_name": r[2], "object_type": r[3], "definition": r[4]
                    }
                    for r in obj_rows
                ]

                # 2. Columns
                cur.execute(PG_COLUMN_SQL)
                col_rows = cur.fetchall()
                cols = [
                    {
                        "schema_name": r[0], "object_name": r[1], "column_name": r[2],
                        "column_id": r[3], "declared_data_type": r[4], "system_data_type": r[5],
                        "is_user_defined": r[6], "max_length": r[7], "precision": r[8],
                        "scale": r[9], "is_nullable": r[10], "is_identity": r[11],
                        "is_computed": r[12], "default_definition": r[13], "collation_name": r[14]
                    }
                    for r in col_rows
                ]

                # 3. Dependencies
                try:
                    cur.execute(PG_DEPENDENCY_SQL)
                    dep_rows = cur.fetchall()
                    deps = [
                        {
                            "referencing_schema_name": r[0], "referencing_entity_name": r[1],
                            "referenced_server_name": r[2], "referenced_database_name": r[3],
                            "referenced_schema_name": r[4], "referenced_entity_name": r[5],
                            "referenced_column_name": r[6], "referenced_minor_id": r[7],
                            "dependency_scope": r[8], "is_schema_bound_reference": r[9],
                            "is_caller_dependent": r[10], "is_ambiguous": r[11]
                        }
                        for r in dep_rows
                    ]
                except Exception:
                    deps = []

                # 4. Key Constraints
                try:
                    cur.execute(PG_KEY_CONSTRAINT_SQL)
                    kc_rows = cur.fetchall()
                    key_constraints = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "constraint_type": r[3], "key_ordinal": r[4], "column_name": r[5]
                        }
                        for r in kc_rows
                    ]
                except Exception:
                    key_constraints = []

                # 5. Foreign Keys
                try:
                    cur.execute(PG_FOREIGN_KEY_SQL)
                    fk_rows = cur.fetchall()
                    foreign_keys = [
                        {
                            "schema_name": r[0], "object_name": r[1], "constraint_name": r[2],
                            "ordinal": r[3], "column_name": r[4], "referenced_schema": r[5],
                            "referenced_object": r[6], "referenced_column": r[7]
                        }
                        for r in fk_rows
                    ]
                except Exception:
                    foreign_keys = []

                # 6. Table Stats
                try:
                    cur.execute(PG_TABLE_STATS_SQL)
                    stat_rows = cur.fetchall()
                    table_stats = [
                        {"schema_name": r[0], "object_name": r[1], "approx_row_count": r[2]}
                        for r in stat_rows
                    ]
                except Exception:
                    table_stats = []

            by: dict[tuple[str, str], list[dict[str, Any]]] = {(r["schema_name"], r["object_name"]): [] for r in objs}
            for c in cols:
                by.setdefault((c["schema_name"], c["object_name"]), []).append({
                    "name": c["column_name"],
                    "ordinal": c["column_id"],
                    "type": c["declared_data_type"] or c["system_data_type"],
                    "declared_type": c["declared_data_type"],
                    "system_type": c["system_data_type"],
                    "is_user_defined": bool(c["is_user_defined"]),
                    "max_length": c["max_length"],
                    "precision": c["precision"],
                    "scale": c["scale"],
                    "nullable": bool(c["is_nullable"]),
                    "identity": bool(c["is_identity"]),
                    "computed": bool(c["is_computed"]),
                    "default": c["default_definition"],
                    "collation": c["collation_name"],
                })

            dep_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for d in deps:
                dep_by.setdefault((d["referencing_schema_name"], d["referencing_entity_name"]), []).append({
                    "server": d["referenced_server_name"],
                    "database": d["referenced_database_name"],
                    "schema": d["referenced_schema_name"],
                    "object": d["referenced_entity_name"],
                    "column": d["referenced_column_name"],
                    "referenced_minor_id": d["referenced_minor_id"],
                    "type": d["dependency_scope"],
                    "is_schema_bound_reference": bool(d["is_schema_bound_reference"]),
                    "is_caller_dependent": bool(d["is_caller_dependent"]),
                    "is_ambiguous": bool(d["is_ambiguous"]),
                })

            constraint_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            grouped_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
            for k in key_constraints:
                key = (k["schema_name"], k["object_name"], k["constraint_name"])
                row = grouped_keys.setdefault(key, {
                    "name": k["constraint_name"],
                    "type": "PRIMARY_KEY" if str(k["constraint_type"]).upper().startswith("PRIMARY") else "UNIQUE",
                    "columns": [],
                })
                row["columns"].append(k["column_name"])
            for (sch, obj, _), row in grouped_keys.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            grouped_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
            for f in foreign_keys:
                key = (f["schema_name"], f["object_name"], f["constraint_name"])
                row = grouped_fks.setdefault(key, {
                    "name": f["constraint_name"], "type": "FOREIGN_KEY", "columns": [],
                    "referenced_schema": f["referenced_schema"], "referenced_object": f["referenced_object"],
                    "referenced_columns": [],
                })
                row["columns"].append(f["column_name"])
                row["referenced_columns"].append(f["referenced_column"])
            for (sch, obj, _), row in grouped_fks.items():
                constraint_by.setdefault((sch, obj), []).append(row)

            stats_by = {(r["schema_name"], r["object_name"]): int(r["approx_row_count"] or 0) for r in table_stats}

            return {
                "database": objs[0]["database_name"] if objs else cfg.get("dbname", ""),
                "objects": [{
                    "database": r["database_name"],
                    "schema": r["schema_name"],
                    "name": r["object_name"],
                    "type": r["object_type"],
                    "definition": r["definition"],
                    "columns": by.get((r["schema_name"], r["object_name"]), []),
                    "dependencies": dep_by.get((r["schema_name"], r["object_name"]), []),
                    "parameters": [],
                    "constraints": constraint_by.get((r["schema_name"], r["object_name"]), []),
                    "approx_row_count": stats_by.get((r["schema_name"], r["object_name"]))
                } for r in objs]
            }
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


# ==============================================================================
# SQL SERVER DISCOVERY WRAPPER (Backwards Compatibility)
# ==============================================================================

def test_sqlserver_connection(connection_string: str) -> dict[str, Any]:
    try:
        import pyodbc
    except Exception as e:
        raise RuntimeError("pyodbc is required for live SQL Server connectivity") from e
    try:
        with pyodbc.connect(connection_string, timeout=10) as conn:
            cur = conn.cursor()
            row = cur.execute("SELECT @@SERVERNAME AS server_name, DB_NAME() AS database_name, CAST(SERVERPROPERTY('ProductVersion') AS varchar(128)) AS product_version").fetchone()
            return {"ok": True, "server": row.server_name, "database": row.database_name, "product_version": row.product_version}
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


def discover_sqlserver(connection_string: str) -> dict[str, Any]:
    try:
        import pyodbc
    except Exception as e:
        raise RuntimeError("pyodbc is required for live SQL Server discovery") from e
    try:
        with pyodbc.connect(connection_string, timeout=20) as conn:
            cur = conn.cursor()
            objs = cur.execute(DISCOVERY_SQL).fetchall()
            cols = cur.execute(COLUMN_SQL).fetchall()
            deps = cur.execute(DEPENDENCY_SQL).fetchall()
            params = cur.execute(PARAMETER_SQL).fetchall()
            try:
                key_constraints = cur.execute(KEY_CONSTRAINT_SQL).fetchall()
            except Exception:
                key_constraints = []
            try:
                foreign_keys = cur.execute(FOREIGN_KEY_SQL).fetchall()
            except Exception:
                foreign_keys = []
            try:
                table_stats = cur.execute(TABLE_STATS_SQL).fetchall()
            except Exception:
                table_stats = []
            by = {(r.schema_name, r.object_name): [] for r in objs}
            for c in cols:
                by.setdefault((c.schema_name, c.object_name), []).append({
                    "name": c.column_name, "ordinal": c.column_id,
                    "type": (c.system_data_type if bool(c.is_user_defined) and c.system_data_type else c.declared_data_type),
                    "declared_type": c.declared_data_type, "system_type": c.system_data_type, "is_user_defined": bool(c.is_user_defined),
                    "max_length": c.max_length, "precision": c.precision, "scale": c.scale,
                    "nullable": bool(c.is_nullable), "identity": bool(c.is_identity),
                    "computed": bool(c.is_computed), "default": c.default_definition,
                    "collation": c.collation_name
                })
            dep_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for d in deps:
                dep_by.setdefault((d.referencing_schema_name, d.referencing_entity_name), []).append({
                    "server": d.referenced_server_name,
                    "database": d.referenced_database_name,
                    "schema": d.referenced_schema_name,
                    "object": d.referenced_entity_name,
                    "column": d.referenced_column_name,
                    "referenced_minor_id": d.referenced_minor_id,
                    "type": d.dependency_scope,
                    "is_schema_bound_reference": bool(d.is_schema_bound_reference),
                    "is_caller_dependent": bool(d.is_caller_dependent),
                    "is_ambiguous": bool(d.is_ambiguous),
                })
            par_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for p in params:
                par_by.setdefault((p.schema_name, p.object_name), []).append({
                    "name": p.parameter_name, "ordinal": p.parameter_id, "type": p.data_type,
                    "max_length": p.max_length, "precision": p.precision, "scale": p.scale,
                    "is_output": bool(p.is_output)
                })
            constraint_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
            grouped_keys: dict[tuple[str, str, str], dict[str, Any]] = {}
            for k in key_constraints:
                key = (k.schema_name, k.object_name, k.constraint_name)
                row = grouped_keys.setdefault(key, {
                    "name": k.constraint_name,
                    "type": "PRIMARY_KEY" if str(k.constraint_type).upper().startswith("PRIMARY") else "UNIQUE",
                    "columns": [],
                })
                row["columns"].append(k.column_name)
            for (sch, obj, _), row in grouped_keys.items():
                constraint_by.setdefault((sch, obj), []).append(row)
            grouped_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
            for f in foreign_keys:
                key = (f.schema_name, f.object_name, f.constraint_name)
                row = grouped_fks.setdefault(key, {
                    "name": f.constraint_name, "type": "FOREIGN_KEY", "columns": [],
                    "referenced_schema": f.referenced_schema, "referenced_object": f.referenced_object,
                    "referenced_columns": [],
                })
                row["columns"].append(f.column_name)
                row["referenced_columns"].append(f.referenced_column)
            for (sch, obj, _), row in grouped_fks.items():
                constraint_by.setdefault((sch, obj), []).append(row)
            stats_by = {(r.schema_name, r.object_name): int(r.approx_row_count or 0) for r in table_stats}
            return {
                "database": objs[0].database_name if objs else "",
                "objects": [{
                    "database": r.database_name, "schema": r.schema_name, "name": r.object_name,
                    "type": r.object_type, "definition": r.definition,
                    "columns": by.get((r.schema_name, r.object_name), []),
                    "dependencies": dep_by.get((r.schema_name, r.object_name), []),
                    "parameters": par_by.get((r.schema_name, r.object_name), []),
                    "constraints": constraint_by.get((r.schema_name, r.object_name), []),
                    "approx_row_count": stats_by.get((r.schema_name, r.object_name))
                } for r in objs]
            }
    except Exception as e:
        raise RuntimeError(connection_diagnostic(e)) from e


def test_source_connection(conn_info: Any, source_type: str = "ORACLE") -> dict[str, Any]:
    st = (source_type or "ORACLE").upper()
    if st == "ORACLE":
        return test_oracle_connection(conn_info)
    elif st == "MYSQL":
        return test_mysql_connection(conn_info)
    elif st in {"POSTGRESQL", "POSTGRES"}:
        return test_postgres_connection(conn_info)
    return test_sqlserver_connection(conn_info)


def discover_source(conn_info: Any, source_type: str = "ORACLE") -> dict[str, Any]:
    st = (source_type or "ORACLE").upper()
    if st == "ORACLE":
        return discover_oracle(conn_info)
    elif st == "MYSQL":
        return discover_mysql(conn_info)
    elif st in {"POSTGRESQL", "POSTGRES"}:
        return discover_postgres(conn_info)
    return discover_sqlserver(conn_info)


