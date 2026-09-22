from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch

from app.services.rules import (
    map_oracle_type,
    map_source_type,
    rewrite_common_oracle,
    rewrite_source_sql,
)
from app.services.discovery import (
    parse_oracle_conn,
    connection_diagnostic,
    test_source_connection as exec_test_source_connection,
    discover_source,
)
from app.services.type_compatibility import (
    quote_identifier,
    quote_oracle_identifier,
    source_select_expression,
    transport_plan,
)
from types import SimpleNamespace


def test_oracle_data_type_mappings():
    # Number mapping variations
    assert map_oracle_type("NUMBER(3,0)") == "SMALLINT"
    assert map_oracle_type("NUMBER(9,0)") == "INT"
    assert map_oracle_type("NUMBER(18,0)") == "BIGINT"
    assert map_oracle_type("NUMBER(38,0)") == "DECIMAL(38,0)"
    assert map_oracle_type("NUMBER(12,2)") == "DECIMAL(12,2)"
    assert map_oracle_type("NUMBER") == "DECIMAL(38,10)"
    assert map_oracle_type("NUMBER(*,2)") == "DECIMAL(38,2)"

    # Character and LOB mappings
    assert map_oracle_type("VARCHAR2(100)") == "STRING"
    assert map_oracle_type("NVARCHAR2(200)") == "STRING"
    assert map_oracle_type("CLOB") == "STRING"
    assert map_oracle_type("NCLOB") == "STRING"
    assert map_oracle_type("LONG") == "STRING"
    assert map_oracle_type("CHAR(10)") == "STRING"
    assert map_oracle_type("NCHAR(10)") == "STRING"
    assert map_oracle_type("ROWID") == "STRING"
    assert map_oracle_type("UROWID") == "STRING"
    assert map_oracle_type("XMLTYPE") == "STRING"
    assert map_oracle_type("BFILE") == "STRING"

    # Date and Timestamp mappings (Oracle DATE has time component -> TIMESTAMP)
    assert map_oracle_type("DATE") == "TIMESTAMP"
    assert map_oracle_type("TIMESTAMP") == "TIMESTAMP"
    assert map_oracle_type("TIMESTAMP WITH TIME ZONE") == "TIMESTAMP"
    assert map_oracle_type("TIMESTAMP WITH LOCAL TIME ZONE") == "TIMESTAMP"

    # Binary types
    assert map_oracle_type("RAW(16)") == "BINARY"
    assert map_oracle_type("LONG RAW") == "BINARY"
    assert map_oracle_type("BLOB") == "BINARY"

    # Floats
    assert map_oracle_type("BINARY_FLOAT") == "FLOAT"
    assert map_oracle_type("BINARY_DOUBLE") == "DOUBLE"
    assert map_oracle_type("FLOAT") == "FLOAT"


def test_map_source_type_oracle_default():
    assert map_source_type("VARCHAR2(50)", source_type="ORACLE") == "STRING"
    assert map_source_type("NUMBER(10,0)", source_type="ORACLE") == "BIGINT"
    assert map_source_type("NUMBER(4,0)", source_type="ORACLE") == "SMALLINT"
    assert map_source_type("DATE", source_type="ORACLE") == "TIMESTAMP"


def test_oracle_sql_rewrites():
    # NVL -> coalesce
    assert rewrite_common_oracle("SELECT NVL(commission_pct, 0) FROM employees") == "SELECT coalesce(commission_pct, 0) FROM employees"
    
    # NVL2 -> CASE WHEN
    nvl2_sql = "SELECT NVL2(manager_id, 'Has Manager', 'No Manager') FROM employees"
    rewritten_nvl2 = rewrite_common_oracle(nvl2_sql)
    assert "CASE WHEN manager_id IS NOT NULL THEN 'Has Manager' ELSE 'No Manager' END" in rewritten_nvl2

    # SYSDATE / SYSTIMESTAMP -> current_timestamp()
    assert rewrite_common_oracle("SELECT SYSDATE FROM DUAL") == "SELECT current_timestamp() FROM DUAL"
    assert rewrite_common_oracle("SELECT SYSTIMESTAMP FROM DUAL") == "SELECT current_timestamp() FROM DUAL"

    # TRUNC(current_timestamp()) -> current_date()
    assert rewrite_common_oracle("SELECT TRUNC(SYSDATE) FROM DUAL") == "SELECT current_date() FROM DUAL"

    # Double quotes to backticks
    assert rewrite_common_oracle('SELECT "FIRST_NAME", "LAST_NAME" FROM "EMPLOYEES"') == "SELECT `FIRST_NAME`, `LAST_NAME` FROM `EMPLOYEES`"


def test_parse_oracle_conn():
    # URL string
    url = "oracle://hr:hrpass@localhost:1521/ORCLPDB1?schema=HR"
    cfg = parse_oracle_conn(url)
    assert cfg["host"] == "localhost"
    assert cfg["port"] == 1521
    assert cfg["service_name"] == "ORCLPDB1"
    assert cfg["user"] == "hr"
    assert cfg["password"] == "hrpass"
    assert cfg["schema"] == "HR"

    # Dictionary
    d = {"host": "oracleserver", "port": "1521", "service_name": "XE", "username": "admin", "password": "pw"}
    cfg2 = parse_oracle_conn(d)
    assert cfg2["host"] == "oracleserver"
    assert cfg2["port"] == 1521
    assert cfg2["service_name"] == "XE"
    assert cfg2["user"] == "admin"
    assert cfg2["password"] == "pw"


def test_oracle_error_diagnostics():
    err_auth = Exception("ORA-01017: invalid username/password; logon denied")
    assert "AUTHENTICATION_FAILED" in connection_diagnostic(err_auth)

    err_listener = Exception("ORA-12541: TNS:no listener")
    assert "NETWORK_UNREACHABLE" in connection_diagnostic(err_listener)

    err_service = Exception("ORA-12514: TNS:listener does not currently know of service requested")
    assert "DATABASE_ACCESS" in connection_diagnostic(err_service)

    err_timeout = Exception("ORA-12170: TNS:Connect timeout occurred")
    assert "NETWORK_UNREACHABLE" in connection_diagnostic(err_timeout)


def test_oracle_type_compatibility_and_projection():
    c_raw = SimpleNamespace(column_name="DOC_GUID", data_type="raw", precision=None, scale=None)
    assert quote_oracle_identifier("DOC_GUID") == '"DOC_GUID"'
    assert quote_identifier("DOC_GUID", "ORACLE") == '"DOC_GUID"'
    assert source_select_expression(c_raw, "ORACLE") == 'RAWTOHEX("DOC_GUID") AS "DOC_GUID"'

    c_clob = SimpleNamespace(column_name="COMMENTS", data_type="clob", precision=None, scale=None)
    plan = transport_plan(c_clob.data_type)
    assert plan.target_type == "STRING"
