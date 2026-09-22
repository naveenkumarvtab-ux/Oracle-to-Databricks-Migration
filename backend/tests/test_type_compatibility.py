from __future__ import annotations

import sys
from types import SimpleNamespace

from sqlalchemy import select

from app.models.entities import MigrationMapping, MigrationObject
from app.services import deployment
from app.services.engine import add_source, classify_project, create_mappings, ensure_project, ingest_snapshot
from app.services.type_compatibility import (
    classify_execution_error,
    normalize_source_value,
    source_select_expression,
    target_parameter_expression,
    transport_plan,
)


def _col(name: str, dtype: str, precision=None, scale=None):
    return SimpleNamespace(column_name=name, data_type=dtype, precision=precision, scale=scale)


def test_rowversion_transport_is_binary_safe_and_metadata_driven():
    c = _col("AnyVersionColumn", "rowversion")
    plan = transport_plan(c.data_type)
    assert plan.target_type == "BINARY"
    assert plan.strategy == "HEX_STRING_TO_BINARY"
    assert source_select_expression(c) == "CONVERT(VARCHAR(MAX), CONVERT(VARBINARY(MAX), [AnyVersionColumn]), 2) AS [AnyVersionColumn]"
    assert target_parameter_expression(c) == "unhex(?)"
    assert normalize_source_value(c, b"\x00\x00\x00\x00\x00\x00\x01\xff") == "00000000000001ff"
    assert normalize_source_value(c, memoryview(b"\xaa\xbb\x00\x00\x00\x00\x00\x01")) == "aabb000000000001"
    assert normalize_source_value(c, "0x0000000000000A0B") == "0000000000000a0b"


def test_varbinary_and_image_use_same_generic_transport_rule():
    for dtype in ("binary", "varbinary", "image", "timestamp", "rowversion"):
        c = _col("Payload", dtype)
        assert transport_plan(dtype).strategy == "HEX_STRING_TO_BINARY"
        assert target_parameter_expression(c) == "unhex(?)"


def test_governed_complex_types_are_explicit_not_silent():
    assert transport_plan("sql_variant").review_required is True
    assert transport_plan("geometry").review_required is True
    assert transport_plan("made_up_udt").strategy == "GOVERNED_TEXT_FALLBACK"
    assert transport_plan("made_up_udt").review_required is True


def test_binary_mismatch_error_is_actionable_and_deterministic():
    info = classify_execution_error(
        '[DATATYPE_MISMATCH.CAST_WITHOUT_SUGGESTION] Cannot resolve "RV" due to data type mismatch: cannot cast "ARRAY<VOID>" to "BINARY"',
        "BRONZE_LOAD",
    )
    assert info["error_category"] == "LOAD"
    assert info["error_code"] == "BINARY_TRANSPORT_MISMATCH"
    assert info["deterministic_remediation_available"] is True
    assert "HEX_STRING_TO_BINARY" in info["recommended_action"]


def test_binary_op_wrong_type_classified_as_databricks_syntax():
    info = classify_execution_error(
        '[DATATYPE_MISMATCH.BINARY_OP_WRONG_TYPE] Cannot resolve "(first_name +  )" due to data type mismatch: the binary operator requires the input type ("NUMERIC" or "INTERVAL DAY TO SECOND" or "INTERVAL YEAR TO MONTH" or "INTERVAL" or "DECFLOAT"), not "STRING". SQLSTATE: 42K09; line 7 pos 8',
        "EXECUTE_DDL",
    )
    assert info["error_category"] == "DATABRICKS_SYNTAX"
    assert info["error_code"] == "BINARY_OP_WRONG_TYPE"
    assert "||" in info["recommended_action"]


def test_recursive_cte_error_classified_as_databricks_syntax():
    info = classify_execution_error(
        '[TABLE_OR_VIEW_NOT_FOUND] The table or view `OrgChart` cannot be found. Verify the spelling and correctness of the schema and catalog.\nSQLSTATE: 42P01; line 18 pos 15',
        "EXECUTE_DDL",
    )
    assert info["error_category"] == "DATABRICKS_SYNTAX"
    assert info["error_code"] == "RECURSIVE_CTE_SYNTAX"
    assert "WITH RECURSIVE" in info["recommended_action"]




def test_bronze_loader_builds_binary_safe_source_and_target_sql(db, monkeypatch):
    project = ensure_project(db, "Binary transport project")
    source = add_source(db, project.id, "src", "server", "DB1")
    ingest_snapshot(
        db,
        project.id,
        source.id,
        {
            "database": "DB1",
            "objects": [
                {
                    "schema": "sales",
                    "name": "AnyTable",
                    "type": "TABLE",
                    "columns": [
                        {"name": "Id", "type": "int", "nullable": False},
                        {"name": "VersionBytes", "type": "rowversion", "nullable": False},
                    ],
                }
            ],
        },
    )
    classify_project(db, project.id)
    create_mappings(db, project.id, "DEV", "cat_dev")
    obj = db.scalar(select(MigrationObject).where(MigrationObject.project_id == project.id))
    mapping = db.scalar(select(MigrationMapping).where(MigrationMapping.project_id == project.id))

    captured = {"source_sql": None, "insert_sql": None, "payload": None}

    class SourceCursor:
        def __init__(self):
            self.calls = 0

        def execute(self, statement):
            captured["source_sql"] = statement
            return self

        def fetchmany(self, _size):
            self.calls += 1
            # Return raw bytes deliberately: normalize_row must still protect the Databricks bind.
            return [(7, b"\x00\x00\x00\x00\x00\x00\x00\x2a")] if self.calls == 1 else []

    class SourceConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return SourceCursor()

        def close(self):
            pass

    class TargetCursor:
        def executemany(self, statement, payload):
            captured["insert_sql"] = statement
            captured["payload"] = payload

    class TargetConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return TargetCursor()

    from app.services import discovery
    monkeypatch.setattr(discovery, "_get_oracle_connection", lambda *_a, **_k: SourceConnection())
    monkeypatch.setattr(discovery, "_get_mysql_connection", lambda *_a, **_k: SourceConnection())
    monkeypatch.setitem(sys.modules, "pyodbc", SimpleNamespace(connect=lambda *_a, **_k: SourceConnection()))
    monkeypatch.setattr(deployment, "databricks_connection", lambda: TargetConnection())

    result = deployment.load_bronze_table(
        db,
        project.id,
        obj,
        mapping,
        "RUN_TEST",
        batch_size=100,
        max_rows=None,
        load_mode="APPEND",
        replace_existing_data=False,
    )

    assert ("RAWTOHEX(\"VersionBytes\") AS \"VersionBytes\"" in captured["source_sql"] or
            "HEX(`VersionBytes`) AS `VersionBytes`" in captured["source_sql"] or
            "CONVERT(VARCHAR(MAX), CONVERT(VARBINARY(MAX), [VersionBytes]), 2) AS [VersionBytes]" in captured["source_sql"])
    assert "VALUES (?,unhex(?),?, current_timestamp())" in captured["insert_sql"]
    assert captured["payload"][0][0] == 7
    assert captured["payload"][0][1] == "000000000000002a"
    assert captured["payload"][0][2] == "server/DB1"
    assert result["transport"]["binary_safe_columns"] == ["VersionBytes"]


def test_binary_transport_accepts_common_connector_text_forms():
    c = _col("Payload", "varbinary")
    assert normalize_source_value(c, "b'\\x00\\x01\\xff'") == "0001ff"
    assert normalize_source_value(c, r"\x00\x01\xff") == "0001ff"
    assert normalize_source_value(c, "X'0A0B'") == "0a0b"
    assert normalize_source_value(c, "0A-0B-FF") == "0a0bff"
    assert normalize_source_value(c, [0, 1, 255]) == "0001ff"


def test_rowversion_enforces_eight_byte_contract():
    c = _col("VersionBytes", "rowversion")
    try:
        normalize_source_value(c, b"\x00\x01")
        assert False, "expected rowversion length validation failure"
    except ValueError as exc:
        assert "exactly 8 bytes" in str(exc)

def test_binary_transport_rejects_arbitrary_text_with_column_context():
    c = _col("VersionBytes", "varbinary")
    try:
        normalize_source_value(c, "not-hex-data")
        assert False, "expected binary transport validation failure"
    except ValueError as exc:
        msg = str(exc)
        assert "non-hexadecimal" in msg
        assert "VersionBytes" in msg
        assert "runtime_type" not in msg  # sensitive values are not echoed into exception text


def test_non_hex_binary_error_is_classified_for_deterministic_runtime_repair():
    info = classify_execution_error(
        "Binary transport value contains non-hexadecimal characters for column VersionBytes",
        "BRONZE_LOAD",
    )
    assert info["error_category"] == "LOAD"
    assert info["error_code"] == "BINARY_TRANSPORT_INVALID_HEX"
    assert info["deterministic_remediation_available"] is True
