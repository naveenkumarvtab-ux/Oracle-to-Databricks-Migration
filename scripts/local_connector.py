# Local connector agent for MySQL / PostgreSQL / SQL Server data source introspection and streaming.
import argparse
from datetime import date, datetime, time as time_type
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.discovery import (
    discover_source,
    test_source_connection,
    connection_diagnostic,
    _get_oracle_connection,
    parse_oracle_conn,
    _get_mysql_connection,
    parse_mysql_conn,
)
from app.services.type_compatibility import source_select_expression


def quoted(value, db_type="oracle"):
    if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
        raise ValueError("Invalid SQL identifier")
    db = str(db_type).lower()
    if db == "oracle":
        return '"' + value.replace('"', '""') + '"'
    if db == "mysql":
        return '`' + value.replace('`', '``') + '`'
    if db in {"postgresql", "postgres"}:
        return '"' + value.replace('"', '""') + '"'
    return "[" + value.replace("]", "]]") + "]"


def encode_value(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return {"kind": "decimal", "value": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"kind": "bytes", "value": bytes(value).hex()}
    if isinstance(value, (datetime, date, time_type)):
        return {"kind": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    raise ValueError("Unsupported source value type")


class LocalAgent:
    def __init__(self, source_id, server, database, connection_info, db_type=None, is_postgres=None):
        self.source_id, self.server, self.database = source_id, server, database
        self.connection_info = connection_info
        if db_type is not None:
            self.db_type = db_type.lower()
        elif is_postgres is not None:
            self.db_type = "postgresql" if is_postgres else "sqlserver"
        elif isinstance(connection_info, dict) and ("service_name" in connection_info or "sid" in connection_info):
            self.db_type = "oracle"
        elif isinstance(connection_info, dict) and ("dbname" in connection_info or "sslmode" in connection_info):
            self.db_type = "postgresql"
        elif isinstance(connection_info, str) and ("DRIVER=" in connection_info.upper() or "SERVER=" in connection_info.upper() or connection_info == "secret"):
            self.db_type = "sqlserver"
        elif isinstance(connection_info, str) and ("oracle" in connection_info.lower() or "1521" in connection_info):
            self.db_type = "oracle"
        else:
            self.db_type = "oracle"
        self.streams = {}

    def connect(self):
        if self.db_type == "oracle":
            if isinstance(self.connection_info, dict):
                return _get_oracle_connection(self.connection_info)
            return _get_oracle_connection(parse_oracle_conn(self.connection_info))
        elif self.db_type == "mysql":
            if isinstance(self.connection_info, dict):
                return _get_mysql_connection(self.connection_info)
            return _get_mysql_connection(parse_mysql_conn(self.connection_info))
        elif self.db_type in {"postgresql", "postgres"}:
            import psycopg2
            if isinstance(self.connection_info, dict):
                return psycopg2.connect(**self.connection_info)
            return psycopg2.connect(self.connection_info)
        else:
            import pyodbc
            try:
                conn = pyodbc.connect(self.connection_info, timeout=10, autocommit=True)
                conn.timeout = 60
                return conn
            except pyodbc.Error as exc:
                if "IM002" in str(exc):
                    installed = [d for d in pyodbc.drivers() if "SQL Server" in d]
                    for alt in ["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server", "SQL Server"]:
                        if alt in installed and alt not in self.connection_info:
                            alt_cs = re.sub(r"DRIVER=\{[^}]+\}", f"DRIVER={{{alt}}}", self.connection_info)
                            try:
                                conn = pyodbc.connect(alt_cs, timeout=10, autocommit=True)
                                conn.timeout = 60
                                self.connection_info = alt_cs
                                return conn
                            except Exception:
                                continue
                raise

    def cleanup(self, all_streams=False):
        for key, item in list(self.streams.items()):
            if all_streams or time.monotonic() - item[2] > 600:
                try:
                    item[0].close()
                finally:
                    del self.streams[key]

    def handle(self, task):
        if (task.get("source_id"), task.get("server"), task.get("database")) != (
                self.source_id, self.server, self.database):
            raise ValueError("Source identity mismatch; verify the registered source profile")
        op, data = task["operation"], task.get("payload", {})
        self.cleanup()
        if op == "test":
            return test_source_connection(self.connection_info)
        if op == "discover":
            return discover_source(self.connection_info)
        if op == "close":
            item = self.streams.pop(data.get("stream_id"), None)
            if item:
                item[0].close()
            return {"closed": True}
        if op == "fetch":
            stream = self.streams.get(data.get("stream_id"))
            if not stream:
                raise ValueError("Source stream expired; restart the deployment after reviewing partial target data")
            stream[2] = time.monotonic()
            size = min(max(int(data.get("size", 1000)), 1), 1000)
            rows, byte_count = [], 0
            for _ in range(size):
                row = stream[3] if stream[3] is not None else stream[1].fetchone()
                stream[3] = None
                if row is None:
                    break
                encoded = [encode_value(v) for v in row]
                length = len(json.dumps(encoded, allow_nan=False).encode())
                if length > 3 * 1024 * 1024:
                    raise ValueError("Source row exceeds connector 3 MiB row limit")
                if rows and byte_count + length > 3 * 1024 * 1024:
                    stream[3] = row
                    break
                rows.append(encoded)
                byte_count += length
            return {"rows": rows}
        if op not in {"count", "open"}:
            raise ValueError("Unsupported operation")

        schema = data.get("schema") or self.database or "public"
        table = data.get("table")
        table_sql = f"{quoted(schema, self.db_type)}.{quoted(table, self.db_type)}" if schema else quoted(table, self.db_type)
        if len(self.streams) >= 4 and op == "open":
            raise ValueError("Connector stream capacity reached")
        conn = self.connect()
        try:
            cur = conn.cursor()
            if self.db_type == "mysql":
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                    (schema, table),
                )
                if not cur.fetchone():
                    raise ValueError(f"Source table '{schema}.{table}' not found or read permission missing")
                if op == "count":
                    cur.execute(f"SELECT COUNT(*) FROM {table_sql}")
                    row = cur.fetchone()
                    return {"count": int(row[0]) if row else 0}
                cur.execute(
                    "SELECT column_name, data_type, numeric_precision, numeric_scale "
                    "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                    (schema, table),
                )
                metadata = cur.fetchall()
                columns = {
                    r[0]: SimpleNamespace(
                        column_name=r[0], data_type=r[1], precision=r[2], scale=r[3]
                    )
                    for r in metadata
                }
                requested = data.get("columns")
                if not isinstance(requested, list) or not requested or any(name not in columns for name in requested):
                    raise ValueError("Source columns changed; repeat discovery before deployment")
                limit = data.get("max_rows")
                limit_clause = f" LIMIT {int(limit)}" if limit is not None and isinstance(limit, int) and limit > 0 else ""
                projection = ", ".join(source_select_expression(columns[name], "MYSQL") for name in requested)
                cur.execute(f"SELECT {projection} FROM {table_sql}{limit_clause}")
            elif self.db_type in {"postgresql", "postgres"}:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                    (schema, table),
                )
                if not cur.fetchone():
                    raise ValueError("Source table not found or read permission missing")
                if op == "count":
                    cur.execute(f"SELECT COUNT(*) FROM {table_sql}")
                    return {"count": int(cur.fetchone()[0])}
                cur.execute(
                    "SELECT column_name, data_type, numeric_precision, numeric_scale "
                    "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                    (schema, table),
                )
                metadata = cur.fetchall()
                columns = {
                    r[0]: SimpleNamespace(
                        column_name=r[0], data_type=r[1], precision=r[2], scale=r[3]
                    )
                    for r in metadata
                }
                requested = data.get("columns")
                if not isinstance(requested, list) or not requested or any(name not in columns for name in requested):
                    raise ValueError("Source columns changed; repeat discovery before deployment")
            elif self.db_type == "oracle":
                cur.execute(
                    "SELECT 1 FROM ALL_TABLES WHERE UPPER(OWNER)=:1 AND UPPER(TABLE_NAME)=:2",
                    [schema.upper(), table.upper()],
                )
                if not cur.fetchone():
                    raise ValueError(f"Source table '{schema}.{table}' not found or read permission missing")
                if op == "count":
                    cur.execute(f"SELECT COUNT(*) FROM {table_sql}")
                    row = cur.fetchone()
                    return {"count": int(row[0]) if row else 0}
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, DATA_PRECISION, DATA_SCALE "
                    "FROM ALL_TAB_COLUMNS WHERE UPPER(OWNER)=:1 AND UPPER(TABLE_NAME)=:2 ORDER BY COLUMN_ID",
                    [schema.upper(), table.upper()],
                )
                metadata = cur.fetchall()
                columns = {
                    r[0]: SimpleNamespace(
                        column_name=r[0], data_type=r[1], precision=r[2], scale=r[3]
                    )
                    for r in metadata
                }
                requested = data.get("columns")
                if not isinstance(requested, list) or not requested or any(name not in columns for name in requested):
                    raise ValueError("Source columns changed; repeat discovery before deployment")
                limit = data.get("max_rows")
                limit_clause = f" FETCH FIRST {int(limit)} ROWS ONLY" if limit is not None and isinstance(limit, int) and limit > 0 else ""
                projection = ", ".join(source_select_expression(columns[name], "ORACLE") for name in requested)
                cur.execute(f"SELECT {projection} FROM {table_sql}{limit_clause}")
            else:
                found = cur.execute(
                    "SELECT t.object_id FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id "
                    "WHERE s.name=? AND t.name=? AND t.is_ms_shipped=0",
                    schema, table,
                ).fetchone()
                if not found:
                    raise ValueError("Source table not found or read permission missing")
                if op == "count":
                    return {"count": int(cur.execute("SELECT COUNT_BIG(*) FROM " + table_sql).fetchone()[0])}
                metadata = cur.execute(
                    "SELECT c.name, CASE WHEN t.is_user_defined=1 THEN TYPE_NAME(c.system_type_id) "
                    "ELSE t.name END, c.precision, c.scale FROM sys.columns c "
                    "JOIN sys.types t ON t.user_type_id=c.user_type_id "
                    "WHERE c.object_id=? AND c.is_computed=0",
                    found[0],
                ).fetchall()
                columns = {
                    r[0]: SimpleNamespace(
                        column_name=r[0], data_type=r[1], precision=r[2], scale=r[3]
                    )
                    for r in metadata
                }
                requested = data.get("columns")
                if not isinstance(requested, list) or not requested or any(name not in columns for name in requested):
                    raise ValueError("Source columns changed; repeat discovery before deployment")
                limit = data.get("max_rows")
                top = f"TOP ({limit}) " if limit is not None else ""
                projection = ",".join(source_select_expression(columns[name], "SQLSERVER") for name in requested)
                cur.execute("SELECT " + top + projection + " FROM " + table_sql)

            stream_id = secrets.token_hex(24)
            self.streams[stream_id] = [conn, cur, time.monotonic(), None]
            conn = None
            return {"stream_id": stream_id}
        finally:
            if conn is not None:
                conn.close()


def validate_url(url):
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or (parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1"}):
        raise ValueError("Connector requires an HTTPS application URL without embedded credentials (or http for localhost)")
    return url.rstrip("/") + "/api"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, default=1521)
    parser.add_argument("--source-type", default="oracle")
    parser.add_argument("--service-name", default=None)
    parser.add_argument("--sid", default=None)
    parser.add_argument("--schema", default=None)
    parser.add_argument("--sslmode", default=None)
    parser.add_argument("--driver", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--trust-server-certificate", action="store_true")
    args = parser.parse_args()

    base = validate_url(args.url)
    token = os.environ.get("CONNECTOR_TOKEN") or getpass.getpass("Connector registration token: ")

    source_type = (args.source_type or ("sqlserver" if args.driver and "sql server" in args.driver.lower() else "oracle")).lower()
    
    if source_type == "oracle":
        user = args.username or os.environ.get("ORACLE_USERNAME") or "system"
        password = os.environ.get("CONNECTOR_ORACLE_PASSWORD") or os.environ.get("ORACLE_PASSWORD")
        if password is None:
            password = getpass.getpass(f"Oracle password for '{user}': ")
        connection_info = {
            "host": args.server,
            "port": args.port or 1521,
            "service_name": args.service_name or args.database,
            "sid": args.sid,
            "schema": args.schema or args.database,
            "user": user,
            "password": password,
        }
        db_type = "Oracle"
    elif source_type == "mysql":
        user = args.username or os.environ.get("MYSQL_USERNAME") or "root"
        password = os.environ.get("CONNECTOR_MYSQL_PASSWORD") or os.environ.get("MYSQL_PASSWORD")
        if password is None:
            password = getpass.getpass(f"MySQL password for '{user}': ")
        connection_info = {
            "host": args.server,
            "port": args.port or 3306,
            "database": args.database,
            "user": user,
            "password": password,
        }
        db_type = "MySQL"
    elif source_type in {"postgresql", "postgres"}:
        user = args.username or os.environ.get("POSTGRES_USERNAME") or "postgres"
        password = os.environ.get("CONNECTOR_PG_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
        if password is None:
            password = getpass.getpass(f"PostgreSQL password for '{user}': ")
        connection_info = {
            "host": args.server,
            "port": args.port or 5432,
            "dbname": args.database,
            "user": user,
            "password": password,
            "sslmode": args.sslmode or "prefer",
        }
        db_type = "PostgreSQL"
    else:
        credentials = "Trusted_Connection=yes;"
        if args.username:
            password = os.environ.get("CONNECTOR_SQL_PASSWORD")
            if password is None:
                password = getpass.getpass("SQL Server password: ")
            credentials = f"UID={args.username};PWD={password};"
        connection_info = (
            f"DRIVER={{{args.driver or 'ODBC Driver 18 for SQL Server'}}};SERVER={{{args.server}}};"
            f"DATABASE={{{args.database}}};{credentials}Encrypt=yes;"
            f"TrustServerCertificate={'yes' if args.trust_server_certificate else 'no'};"
        )
        db_type = "SQL Server"

    agent = LocalAgent(args.source, args.server, args.database, connection_info, db_type=source_type)
    import httpx
    stopped = threading.Event()
    instance_id = secrets.token_hex(16)
    print(f"{db_type} connector started. Keep this process running; database credentials stay on this machine.")
    try:
        with httpx.Client(headers={"Authorization": "Bearer " + token, "X-Connector-Instance": instance_id}, timeout=30, follow_redirects=False) as client:
            def keep_alive():
                while not stopped.wait(15):
                    try:
                        heartbeat = client.post(base + "/connector/heartbeat")
                        if heartbeat.status_code in {401, 403, 409}:
                            print("Connector registration revoked or another instance is active. Stopping.")
                            stopped.set()
                    except httpx.HTTPError:
                        pass
            worker = threading.Thread(target=keep_alive, daemon=True)
            worker.start()
            while not stopped.is_set():
                agent.cleanup()
                try:
                    response = client.post(base + "/connector/poll")
                    if response.status_code in {401, 403, 409}:
                        raise SystemExit("Registration rejected, revoked, or another connector is active. Check Sources.")
                    response.raise_for_status()
                    task = response.json().get("task")
                    if task:
                        try:
                            result = agent.handle(task)
                            ok = True
                            if len(json.dumps(result, allow_nan=False).encode()) > 3500000:
                                raise ValueError("Result exceeds connector payload limit; reduce the source scope")
                        except Exception as error:
                            ok = False
                            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else connection_diagnostic(error)
                            result = {"error": message}
                        payload = {"lease": task["lease"], "ok": ok, "result": result}
                        for attempt in range(3):
                            try:
                                reply = client.post(base + f"/connector/tasks/{task['id']}/result", json=payload)
                                if reply.status_code in {404, 409}:
                                    break
                                reply.raise_for_status()
                                break
                            except httpx.HTTPError:
                                if attempt == 2:
                                    raise
                                time.sleep(1)
                        print(f"{task['operation']}: {'completed' if ok else 'failed'}")
                    else:
                        time.sleep(2)
                except httpx.HTTPError:
                    print("Hosted application unreachable. Retrying in 5 seconds.")
                    time.sleep(5)
    except KeyboardInterrupt:
        print("Connector stopped.")
    finally:
        stopped.set()
        agent.cleanup(all_streams=True)


if __name__ == "__main__":
    main()
