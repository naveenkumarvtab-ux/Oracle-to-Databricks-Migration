from __future__ import annotations

"""Metadata-driven SQL Server -> Databricks runtime compatibility framework.

This module is intentionally independent from object/column names.  Discovery metadata
selects a type-family adapter.  The adapter controls:

* source-side projection/extraction
* connector-safe canonical transport representation
* target parameter expression
* local validation before Databricks execution
* governed fallback/review policy for ambiguous source types

The design prevents one-off fixes such as ``if column_name == 'RowVersion'`` and makes
runtime behavior reusable across projects, databases, schemas and customers.
"""

from array import array as py_array
import ast
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID

from .rules import map_sqlserver_type


BINARY_SOURCE_TYPES = {"binary", "varbinary", "image", "timestamp", "rowversion", "bytea", "blob", "tinyblob", "mediumblob", "longblob", "raw", "long raw"}
INTEGER_SOURCE_TYPES = {
    "bigint", "int", "integer", "mediumint", "smallint", "tinyint", "year",
    "int2", "int4", "int8", "serial", "bigserial", "smallserial",
    "serial2", "serial4", "serial8"
}
FLOAT_SOURCE_TYPES = {"float", "float4", "float8", "real", "double precision", "double", "binary_float", "binary_double"}
DECIMAL_SOURCE_TYPES = {"decimal", "numeric", "money", "smallmoney", "number", "dec", "fixed"}
STRING_SOURCE_TYPES = {
    "char", "varchar", "character varying", "character", "text", "tinytext", "mediumtext", "longtext",
    "varchar2", "nvarchar2", "clob", "nclob", "long", "rowid", "urowid", "bfile", "xmltype",
    "nchar", "nvarchar", "ntext", "sysname", "json", "jsonb", "citext", "name", "bpchar",
    "inet", "cidr", "macaddr", "macaddr8", "tsvector", "tsquery", "interval", "enum", "set"
}
DATE_SOURCE_TYPES = {"date"}
DATETIME_SOURCE_TYPES = {
    "datetime", "datetime2", "smalldatetime", "timestamptz",
    "timestamp with time zone", "timestamp without time zone",
    "timestamp with local time zone"
}
GOVERNED_TEXT_TYPES = {
    "time", "timetz", "time with time zone", "time without time zone",
    "datetimeoffset", "sql_variant", "xml", "hierarchyid"
}
SPATIAL_SOURCE_TYPES = {
    "geography", "geometry", "point", "line", "linestring", "lseg", "box",
    "path", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection", "circle"
}

KNOWN_SOURCE_TYPES = (
    BINARY_SOURCE_TYPES
    | INTEGER_SOURCE_TYPES
    | FLOAT_SOURCE_TYPES
    | DECIMAL_SOURCE_TYPES
    | STRING_SOURCE_TYPES
    | DATE_SOURCE_TYPES
    | DATETIME_SOURCE_TYPES
    | GOVERNED_TEXT_TYPES
    | SPATIAL_SOURCE_TYPES
    | {"bit", "bool", "boolean", "uniqueidentifier"}
)


class TransportNormalizationError(ValueError):
    """Structured, non-sensitive runtime transport failure.

    The exception deliberately records metadata and runtime shape but not source values.
    This lets deployment evidence be actionable without leaking customer data to logs.
    """

    def __init__(
        self,
        message: str,
        *,
        column_name: str | None = None,
        source_type: str | None = None,
        target_type: str | None = None,
        adapter_id: str | None = None,
        runtime_type: str | None = None,
        value_length: int | None = None,
        row_index: int | None = None,
        error_code: str = "TRANSPORT_NORMALIZATION_FAILED",
    ) -> None:
        super().__init__(message)
        self.column_name = column_name
        self.source_type = source_type
        self.target_type = target_type
        self.adapter_id = adapter_id
        self.runtime_type = runtime_type
        self.value_length = value_length
        self.row_index = row_index
        self.error_code = error_code

    def diagnostics(self) -> dict[str, Any]:
        return {
            "column_name": self.column_name,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "adapter_id": self.adapter_id,
            "runtime_type": self.runtime_type,
            "value_length": self.value_length,
            "row_index": self.row_index,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class TransportPlan:
    source_type: str
    target_type: str
    strategy: str
    parameter_expression: str = "?"
    review_required: bool = False
    notes: str = ""
    adapter_id: str = ""
    family: str = ""
    deterministic: bool = True
    source_projection: str = "NATIVE"


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    family: str
    source_types: frozenset[str]
    strategy: str
    parameter_expression: str = "?"
    review_required: bool = False
    deterministic: bool = True
    notes: str = ""
    source_projection: str = "NATIVE"


ADAPTER_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        "binary.hex.v2", "BINARY", frozenset(BINARY_SOURCE_TYPES),
        "HEX_STRING_TO_BINARY", "unhex(?)", False, True,
        "Binary values are source-projected as hexadecimal text and rebuilt with Databricks unhex().",
        "HEX_TEXT",
    ),
    AdapterSpec("boolean.native.v1", "BOOLEAN", frozenset({"bit", "bool", "boolean"}), "BOOLEAN_NATIVE"),
    AdapterSpec("integer.native.v1", "INTEGER", frozenset(INTEGER_SOURCE_TYPES), "INTEGER_NATIVE"),
    AdapterSpec("float.native.v1", "FLOAT", frozenset(FLOAT_SOURCE_TYPES), "FLOAT_NATIVE"),
    AdapterSpec("decimal.native.v2", "DECIMAL", frozenset(DECIMAL_SOURCE_TYPES), "DECIMAL_NATIVE"),
    AdapterSpec("uuid.string.v1", "UUID", frozenset({"uniqueidentifier", "uuid"}), "UUID_STRING", source_projection="TEXT"),
    AdapterSpec("date.native.v1", "DATE", frozenset(DATE_SOURCE_TYPES), "DATE_NATIVE"),
    AdapterSpec("datetime.native.v1", "DATETIME", frozenset(DATETIME_SOURCE_TYPES), "DATETIME_NATIVE"),
    AdapterSpec("time.iso.v1", "TIME", frozenset({"time", "timetz", "time with time zone", "time without time zone"}), "TIME_ISO_STRING", notes="Preserves time without inventing timezone semantics."),
    AdapterSpec("datetimeoffset.iso.v1", "DATETIMEOFFSET", frozenset({"datetimeoffset"}), "DATETIMEOFFSET_ISO_STRING", notes="Preserves the source offset exactly as ISO text."),
    AdapterSpec("xml.string.v1", "XML", frozenset({"xml"}), "XML_STRING", source_projection="TEXT"),
    AdapterSpec(
        "sqlvariant.governed.v1", "SQL_VARIANT", frozenset({"sql_variant"}), "SQL_VARIANT_STRING",
        review_required=True, deterministic=True,
        notes="sql_variant is preserved textually; semantic retargeting requires review.", source_projection="TEXT",
    ),
    AdapterSpec("hierarchyid.string.v1", "HIERARCHYID", frozenset({"hierarchyid"}), "HIERARCHYID_STRING", source_projection="TOSTRING"),
    AdapterSpec(
        "spatial.wkt.v1", "SPATIAL", frozenset(SPATIAL_SOURCE_TYPES), "SPATIAL_SRID_WKT_STRING",
        review_required=True, deterministic=True,
        notes="Spatial values are preserved as SRID + WKT until a project-approved geospatial target strategy is chosen.",
        source_projection="SRID_WKT",
    ),
    AdapterSpec("string.native.v1", "STRING", frozenset(STRING_SOURCE_TYPES), "STRING_NATIVE"),
)

_SPEC_BY_TYPE = {source_type: spec for spec in ADAPTER_SPECS for source_type in spec.source_types}


def normalize_source_type(name: str | None) -> str:
    text = (name or "").strip().lower().replace("[", "").replace("]", "").replace('"', '')
    # Defensive support for imported declarations such as decimal(18,2), varchar(max), time(7).
    return re.sub(r"\s*\(.*\)\s*$", "", text)


def quote_oracle_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_mysql_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_postgres_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_sqlserver_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def quote_identifier(name: str, source_type: str = "SQLSERVER") -> str:
    st = (source_type or "SQLSERVER").upper()
    if st == "ORACLE":
        return quote_oracle_identifier(name)
    elif st == "MYSQL":
        return quote_mysql_identifier(name)
    elif st in {"POSTGRESQL", "POSTGRES"}:
        return quote_postgres_identifier(name)
    return quote_sqlserver_identifier(name)


def adapter_spec(source_type: str | None) -> AdapterSpec:
    src = normalize_source_type(source_type)
    spec = _SPEC_BY_TYPE.get(src)
    if spec:
        return spec
    # User-defined aliases and unknown types are never silently guessed.  They use a
    # reversible textual transport where the source can convert them, and are surfaced
    # for architecture review before semantic retargeting.
    return AdapterSpec(
        "unknown.governed_text.v1", "UNKNOWN", frozenset(), "GOVERNED_TEXT_FALLBACK",
        review_required=True, deterministic=True,
        notes="Unknown/user-defined source type uses governed text fallback; approve target semantics before production promotion.",
        source_projection="TEXT",
    )


def transport_plan(source_type: str, precision: int | None = None, scale: int | None = None) -> TransportPlan:
    src = normalize_source_type(source_type)
    spec = adapter_spec(src)
    target = map_sqlserver_type(src, precision, scale)
    # Deterministic target overrides for governed families.
    if spec.family == "BINARY":
        target = "BINARY"
    elif spec.family in {"UUID", "TIME", "DATETIMEOFFSET", "XML", "SQL_VARIANT", "HIERARCHYID", "SPATIAL", "UNKNOWN", "STRING"}:
        target = "STRING"
    return TransportPlan(
        source_type=src or "unknown",
        target_type=target or "STRING",
        strategy=spec.strategy,
        parameter_expression=spec.parameter_expression,
        review_required=spec.review_required,
        notes=spec.notes,
        adapter_id=spec.adapter_id,
        family=spec.family,
        deterministic=spec.deterministic,
        source_projection=spec.source_projection,
    )


def source_select_expression(column: Any, source_type: str = "SQLSERVER") -> str:
    """Return a source-side projection selected only from discovered metadata."""
    col_name = str(column.column_name)
    st = (source_type or "SQLSERVER").upper()
    is_oracle = st == "ORACLE"
    is_mysql = st == "MYSQL"
    is_pg = st in {"POSTGRESQL", "POSTGRES"}
    if is_oracle:
        quoted_name = quote_oracle_identifier(col_name)
    elif is_mysql:
        quoted_name = quote_mysql_identifier(col_name)
    elif is_pg:
        quoted_name = quote_postgres_identifier(col_name)
    else:
        quoted_name = quote_sqlserver_identifier(col_name)
        
    plan = transport_plan(column.data_type, getattr(column, "precision", None), getattr(column, "scale", None))
    
    if plan.strategy == "HEX_STRING_TO_BINARY":
        if is_oracle:
            return f"RAWTOHEX({quoted_name}) AS {quoted_name}"
        if is_mysql:
            return f"HEX({quoted_name}) AS {quoted_name}"
        if is_pg:
            return f"encode({quoted_name}, 'hex') AS {quoted_name}"
        return f"CONVERT(VARCHAR(MAX), CONVERT(VARBINARY(MAX), {quoted_name}), 2) AS {quoted_name}"
    
    if plan.strategy == "UUID_STRING":
        if is_oracle:
            return f"TO_CHAR({quoted_name}) AS {quoted_name}"
        if is_mysql:
            return f"CAST({quoted_name} AS CHAR) AS {quoted_name}"
        if is_pg:
            return f"{quoted_name}::text AS {quoted_name}"
        return f"CONVERT(VARCHAR(36), {quoted_name}) AS {quoted_name}"
        
    if plan.strategy in {"XML_STRING", "SQL_VARIANT_STRING", "GOVERNED_TEXT_FALLBACK"}:
        if is_oracle:
            return f"TO_CHAR({quoted_name}) AS {quoted_name}"
        if is_mysql:
            return f"CAST({quoted_name} AS CHAR) AS {quoted_name}"
        if is_pg:
            return f"{quoted_name}::text AS {quoted_name}"
        return f"CONVERT(NVARCHAR(MAX), {quoted_name}) AS {quoted_name}"
        
    if plan.strategy == "HIERARCHYID_STRING":
        return f"CASE WHEN {quoted_name} IS NULL THEN NULL ELSE {quoted_name}.ToString() END AS {quoted_name}"
        
    if plan.strategy == "SPATIAL_SRID_WKT_STRING":
        if is_oracle:
            return f"SDO_UTIL.TO_WKTGEOMETRY({quoted_name}) AS {quoted_name}"
        if is_mysql:
            return f"ST_AsText({quoted_name}) AS {quoted_name}"
        if is_pg:
            return f"{quoted_name}::text AS {quoted_name}"
        return (
            f"CASE WHEN {quoted_name} IS NULL THEN NULL ELSE "
            f"CONCAT('SRID=', {quoted_name}.STSrid, ';', {quoted_name}.STAsText()) END AS {quoted_name}"
        )
    return quoted_name



def target_parameter_expression(column: Any) -> str:
    return transport_plan(column.data_type, getattr(column, "precision", None), getattr(column, "scale", None)).parameter_expression


def _value_length(value: Any) -> int | None:
    try:
        return len(value)  # type: ignore[arg-type]
    except Exception:
        return None


def _transport_error(column: Any, plan: TransportPlan, value: Any, message: str, code: str, row_index: int | None = None) -> TransportNormalizationError:
    column_name = getattr(column, "column_name", None)
    safe_message = message + (f" for column {column_name}" if column_name else "")
    return TransportNormalizationError(
        safe_message,
        column_name=column_name,
        source_type=plan.source_type,
        target_type=plan.target_type,
        adapter_id=plan.adapter_id,
        runtime_type=type(value).__name__,
        value_length=_value_length(value),
        row_index=row_index,
        error_code=code,
    )


def _bytes_from_runtime(value: Any) -> bytes | None:
    """Recognize bounded, lossless binary runtime representations."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, py_array):
        try:
            return value.tobytes()
        except Exception:
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(x, int) and 0 <= x <= 255 for x in value):
        try:
            return bytes(value)
        except Exception:
            return None
    tobytes = getattr(value, "tobytes", None)
    if callable(tobytes):
        try:
            result = tobytes()
            if isinstance(result, (bytes, bytearray, memoryview)):
                return bytes(result)
        except Exception:
            return None
    return None


def _normalize_hex(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> str:
    raw_bytes = _bytes_from_runtime(value)
    if raw_bytes is not None:
        text = raw_bytes.hex()
    elif isinstance(value, str):
        raw = value.strip()
        text = raw
        if text.lower().startswith("0x"):
            text = text[2:]
        m = re.fullmatch(r"(?i)x'([0-9a-f]*)'", text)
        if m:
            text = m.group(1)
        if (text.startswith("b'") and text.endswith("'")) or (text.startswith('b"') and text.endswith('"')):
            try:
                literal = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                literal = None
            if isinstance(literal, bytes):
                text = literal.hex()
        if re.fullmatch(r"(?:\\x[0-9a-fA-F]{2})+", text):
            text = "".join(re.findall(r"\\x([0-9a-fA-F]{2})", text))
        if re.fullmatch(r"[0-9a-fA-F]{2}(?:[\s,:-]+[0-9a-fA-F]{2})+", text):
            text = re.sub(r"[\s,:-]+", "", text)
        if len(text) % 2:
            raise _transport_error(column, plan, value, "Binary hexadecimal transport must contain an even number of characters", "BINARY_HEX_ODD_LENGTH", row_index)
        if text and not re.fullmatch(r"[0-9a-fA-F]+", text):
            raise _transport_error(column, plan, value, "Binary transport value contains non-hexadecimal characters", "BINARY_TRANSPORT_INVALID_HEX", row_index)
    else:
        raise _transport_error(column, plan, value, "Unsupported binary runtime representation", "BINARY_RUNTIME_UNSUPPORTED", row_index)

    # SQL Server rowversion/timestamp is always binary(8).  Enforce that contract so
    # a malformed transport cannot silently load corrupted concurrency tokens.
    if plan.source_type in {"rowversion", "timestamp"} and len(text) != 16:
        raise _transport_error(column, plan, value, "SQL Server rowversion/timestamp must contain exactly 8 bytes", "ROWVERSION_LENGTH_INVALID", row_index)
    return text.lower()


def _normalize_boolean(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "t", "yes", "y"}:
            return True
        if text in {"0", "false", "f", "no", "n"}:
            return False
    raise _transport_error(column, plan, value, "SQL Server bit value could not be normalized deterministically", "BOOLEAN_RUNTIME_INVALID", row_index)


def _normalize_integer(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> int:
    try:
        ivalue = int(value)
    except Exception as exc:
        raise _transport_error(column, plan, value, "Integer value could not be normalized", "INTEGER_RUNTIME_INVALID", row_index) from exc
    limits = {
        "tinyint": (0, 255),
        "smallint": (-32768, 32767),
        "int": (-2147483648, 2147483647),
        "bigint": (-9223372036854775808, 9223372036854775807),
    }
    low, high = limits.get(plan.source_type, (-9223372036854775808, 9223372036854775807))
    if not (low <= ivalue <= high):
        raise _transport_error(column, plan, value, "Integer value is outside the discovered SQL Server type range", "INTEGER_RANGE_INVALID", row_index)
    return ivalue


def _normalize_float(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> float:
    try:
        fvalue = float(value)
    except Exception as exc:
        raise _transport_error(column, plan, value, "Floating-point value could not be normalized", "FLOAT_RUNTIME_INVALID", row_index) from exc
    if math.isnan(fvalue) or math.isinf(fvalue):
        raise _transport_error(column, plan, value, "NaN/Infinity requires an explicit project policy", "FLOAT_NONFINITE_BLOCKED", row_index)
    return fvalue


def _normalize_decimal(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> Decimal:
    try:
        dvalue = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _transport_error(column, plan, value, "Decimal value could not be normalized", "DECIMAL_RUNTIME_INVALID", row_index) from exc
    if not dvalue.is_finite():
        raise _transport_error(column, plan, value, "Non-finite decimal value is not supported", "DECIMAL_NONFINITE_BLOCKED", row_index)
    precision = getattr(column, "precision", None)
    scale = getattr(column, "scale", None)
    if plan.source_type in {"money", "smallmoney"}:
        precision, scale = 19, 4
    if precision:
        sign, digits, exponent = dvalue.as_tuple()
        actual_scale = max(-exponent, 0)
        integer_digits = max(len(digits) - actual_scale, 0)
        if integer_digits + max(actual_scale, scale or 0) > precision:
            raise _transport_error(column, plan, value, "Decimal value exceeds discovered precision", "DECIMAL_PRECISION_OVERFLOW", row_index)
    return dvalue


def _normalize_uuid(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except Exception as exc:
        raise _transport_error(column, plan, value, "uniqueidentifier value is not a valid UUID", "UUID_RUNTIME_INVALID", row_index) from exc


def _normalize_date(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise _transport_error(column, plan, value, "Date value could not be normalized", "DATE_RUNTIME_INVALID", row_index)


def _normalize_datetime(column: Any, plan: TransportPlan, value: Any, row_index: int | None = None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            pass
    raise _transport_error(column, plan, value, "Datetime value could not be normalized", "DATETIME_RUNTIME_INVALID", row_index)


def normalize_source_value(column: Any, value: Any, row_index: int | None = None) -> Any:
    """Normalize one source value using discovered metadata only."""
    if value is None:
        return None
    plan = transport_plan(column.data_type, getattr(column, "precision", None), getattr(column, "scale", None))
    if plan.strategy == "HEX_STRING_TO_BINARY":
        return _normalize_hex(column, plan, value, row_index)
    if plan.strategy == "BOOLEAN_NATIVE":
        return _normalize_boolean(column, plan, value, row_index)
    if plan.strategy == "INTEGER_NATIVE":
        return _normalize_integer(column, plan, value, row_index)
    if plan.strategy == "FLOAT_NATIVE":
        return _normalize_float(column, plan, value, row_index)
    if plan.strategy == "DECIMAL_NATIVE":
        return _normalize_decimal(column, plan, value, row_index)
    if plan.strategy == "UUID_STRING":
        return _normalize_uuid(column, plan, value, row_index)
    if plan.strategy == "DATE_NATIVE":
        return _normalize_date(column, plan, value, row_index)
    if plan.strategy == "DATETIME_NATIVE":
        return _normalize_datetime(column, plan, value, row_index)
    if plan.strategy == "TIME_ISO_STRING":
        return value.isoformat() if isinstance(value, time) else str(value)
    if plan.strategy == "DATETIMEOFFSET_ISO_STRING":
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if plan.strategy in {
        "XML_STRING", "SQL_VARIANT_STRING", "HIERARCHYID_STRING", "SPATIAL_SRID_WKT_STRING",
        "STRING_NATIVE", "GOVERNED_TEXT_FALLBACK",
    }:
        return str(value)
    return value


def normalize_row(columns: Sequence[Any], row: Sequence[Any], row_index: int | None = None) -> tuple[Any, ...]:
    if len(columns) != len(row):
        raise TransportNormalizationError(
            f"Source row width {len(row)} does not match migration metadata width {len(columns)}",
            row_index=row_index,
            error_code="SOURCE_ROW_WIDTH_MISMATCH",
        )
    return tuple(normalize_source_value(column, value, row_index) for column, value in zip(columns, row))


def transport_contract(columns: Iterable[Any]) -> list[dict[str, Any]]:
    contract: list[dict[str, Any]] = []
    for column in columns:
        plan = transport_plan(column.data_type, getattr(column, "precision", None), getattr(column, "scale", None))
        contract.append({
            "column": column.column_name,
            "source_type": normalize_source_type(column.data_type),
            "target_type": plan.target_type,
            "family": plan.family,
            "adapter_id": plan.adapter_id,
            "strategy": plan.strategy,
            "source_projection": plan.source_projection,
            "target_parameter_expression": plan.parameter_expression,
            "deterministic": plan.deterministic,
            "review_required": plan.review_required,
            "notes": plan.notes,
        })
    return contract


def transport_summary(columns: Iterable[Any]) -> dict[str, Any]:
    contract = transport_contract(columns)
    counts: dict[str, int] = {}
    families: dict[str, int] = {}
    for item in contract:
        counts[item["strategy"]] = counts.get(item["strategy"], 0) + 1
        families[item["family"]] = families.get(item["family"], 0) + 1
    deterministic = sum(1 for x in contract if x["deterministic"] and not x["review_required"])
    return {
        "strategy_counts": counts,
        "family_counts": families,
        "deterministic_columns": deterministic,
        "total_columns": len(contract),
        "deterministic_coverage_pct": round((deterministic / len(contract) * 100.0), 2) if contract else 100.0,
        "review_required_columns": [x["column"] for x in contract if x["review_required"]],
        "binary_safe_columns": [x["column"] for x in contract if x["family"] == "BINARY"],
        "unknown_type_columns": [x["column"] for x in contract if x["family"] == "UNKNOWN"],
    }


def compatibility_catalog() -> list[dict[str, Any]]:
    """Expose the deterministic adapter registry for diagnostics/UI documentation."""
    rows = []
    for spec in ADAPTER_SPECS:
        rows.append({
            "adapter_id": spec.adapter_id,
            "family": spec.family,
            "source_types": sorted(spec.source_types),
            "strategy": spec.strategy,
            "source_projection": spec.source_projection,
            "target_parameter_expression": spec.parameter_expression,
            "deterministic": spec.deterministic,
            "review_required": spec.review_required,
            "notes": spec.notes,
        })
    rows.append({
        "adapter_id": "unknown.governed_text.v1",
        "family": "UNKNOWN",
        "source_types": ["<user-defined/unknown>"],
        "strategy": "GOVERNED_TEXT_FALLBACK",
        "source_projection": "NVARCHAR_MAX",
        "target_parameter_expression": "?",
        "deterministic": True,
        "review_required": True,
        "notes": "Unknown/user-defined types are preserved textually and blocked from silent semantic promotion.",
    })
    return rows


def classify_execution_error(exc: Exception | str, stage: str = "UNKNOWN") -> dict[str, Any]:
    text = str(exc)
    low = text.lower()
    upper = text.upper()
    category = "CONVERSION"
    code = "UNCLASSIFIED_EXECUTION_ERROR"
    retryable = False
    deterministic = False
    action = "Review technical details, correct the governed artifact/configuration, then resume from the failed stage."
    diagnostics: dict[str, Any] = {}

    if isinstance(exc, TransportNormalizationError):
        diagnostics = exc.diagnostics()
        category = "LOAD"
        code = exc.error_code
        deterministic = True
        action = (
            "The runtime compatibility adapter rejected a source representation before Databricks execution. "
            "Use the recorded adapter/source/runtime diagnostics, correct or extend the metadata-driven adapter, then resume the failed load."
        )
    elif "BINARY TRANSPORT VALUE CONTAINS NON-HEXADECIMAL CHARACTERS" in upper or "BINARY_TRANSPORT_INVALID_HEX" in upper:
        category, code, deterministic = "LOAD", "BINARY_TRANSPORT_INVALID_HEX", True
        action = "Use the canonical binary adapter (SQL Server VARBINARY -> style-2 hex -> Databricks unhex), validate locally, then resume."
    elif "ARRAY<VOID>" in upper and "BINARY" in upper:
        category, code, deterministic = "LOAD", "BINARY_TRANSPORT_MISMATCH", True
        action = "Use the canonical HEX_STRING_TO_BINARY adapter so connector inference cannot produce ARRAY<VOID>, then resume the load."
    elif "BINARY_OP_WRONG_TYPE" in upper or "STRING_CONCATENATION" in upper:
        category, code = "DATABRICKS_SYNTAX", "BINARY_OP_WRONG_TYPE"
        action = "Rewrite SQL Server operators incompatible with Databricks SQL (e.g. '+' string concatenation to '||' or concat(...)), validate locally, then resume."
    elif "DATATYPE_MISMATCH" in upper or "data type mismatch" in low or "cannot cast" in low:
        category = "LOAD" if "LOAD" in stage.upper() else "TARGET_SCHEMA"
        code, deterministic = "DATATYPE_MISMATCH", True
        action = "Compare discovered source type, generated target type and adapter contract; apply the deterministic compatibility rule and resume."
    elif "unresolved_column" in low or "unresolved column" in low or ("cannot resolve" in low and "column" in low):
        category, code = "UNRESOLVED_COLUMN", "UNRESOLVED_COLUMN"
        action = "Correct the project-scoped object/column mapping or source logic before deployment."
    elif "data source name not found" in low or ("driver" in low and "not found" in low):
        category, code = "CONFIGURATION", "SOURCE_DRIVER_CONFIGURATION"
        action = "Install/select the configured SQL Server ODBC driver and rerun the source connection precheck."
    elif (
        "table_or_view_not_found" in low or "table or view not found" in low or "relation does not exist" in low
        or re.search(r"\b(?:table|view|relation)\b.*\bdoes not exist\b", low)
    ):
        if any(w in upper for w in ("ORGCHART", "CTE", "RECURSIVE")):
            category, code = "DATABRICKS_SYNTAX", "RECURSIVE_CTE_SYNTAX"
            action = "Add RECURSIVE to the WITH clause (e.g. WITH RECURSIVE cte_name AS ...) for self-referencing CTEs, then resume."
        else:
            category, code = "DEPENDENCY", "TARGET_DEPENDENCY_NOT_FOUND"
            action = "Verify project/environment mappings and dependency order, deploy prerequisites, then resume."
    elif "schema drift" in low or "target schema" in low:
        category, code = "TARGET_SCHEMA", "TARGET_SCHEMA_DRIFT"
        action = "Run schema comparison and apply the configured safe ALTER/DEV replacement policy with approval where required."
    elif any(token in low for token in ("parse_syntax_error", "syntax error", "sqlstate: 42601", "mismatched input")):
        category, code = "DATABRICKS_SYNTAX", "DATABRICKS_SYNTAX"
        action = "Regenerate or remediate the artifact, run static validation, obtain approval for the new version, then resume."
    elif any(token in low for token in ("permission denied", "insufficient privileges", "not authorized", "forbidden")):
        category, code = "SECURITY", "TARGET_PERMISSION_DENIED"
        action = "Correct Databricks permissions/ownership; do not bypass governance controls."
    elif any(token in low for token in ("invalid access token", "unauthorized", "authentication", "token expired")):
        category, code = "AUTHENTICATION", "DATABRICKS_AUTHENTICATION"
        action = "Refresh the configured secret/token and retest Databricks connectivity."
    elif any(token in low for token in ("warehouse is starting", "connection reset", "temporary service unavailable", "gateway timeout", "rate limit", "deadlock")):
        category, code, retryable = "TRANSIENT_PLATFORM", "TRANSIENT_DATABRICKS_FAILURE", True
        action = "Retry only the safe/idempotent operation using bounded exponential backoff; resume governed writes from evidence."
    elif any(token in low for token in ("databricks connection is not configured", "not configured", "configuration")):
        category, code = "CONFIGURATION", "CONFIGURATION_ERROR"
        action = "Correct environment configuration/secrets and rerun the precheck."
    elif any(token in low for token in ("connection refused", "network", "timeout", "could not connect")):
        category, code = "CONNECTIVITY", "CONNECTIVITY_ERROR"
        action = "Verify source/target network connectivity and rerun the connection precheck."
    elif any(token in low for token in ("destructive", "explicit approval", "replacement is blocked")):
        category, code = "APPROVAL", "GOVERNED_OPERATION_BLOCKED"
        action = "Obtain the required governed approval/policy setting; do not bypass destructive-operation controls."

    return {
        "error_category": category,
        "error_code": code,
        "stage": stage,
        "retryable": retryable,
        "deterministic_remediation_available": deterministic,
        "recommended_action": action,
        "technical_error": text,
        "diagnostics": diagnostics,
    }
