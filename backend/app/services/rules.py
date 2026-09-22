from __future__ import annotations
import re

TYPE_MAP = {
    # MySQL, SQL Server & PostgreSQL Integer Types
    "bigint": "BIGINT", "int": "INT", "integer": "INT", "mediumint": "INT",
    "smallint": "SMALLINT", "tinyint": "SMALLINT",
    "int2": "SMALLINT", "int4": "INT", "int8": "BIGINT",
    "serial": "INT", "bigserial": "BIGINT", "smallserial": "SMALLINT",
    "serial2": "SMALLINT", "serial4": "INT", "serial8": "BIGINT",
    "year": "INT",

    # Boolean Types
    "bit": "BOOLEAN", "bool": "BOOLEAN", "boolean": "BOOLEAN",

    # Floating point & numeric
    "float": "FLOAT", "float4": "FLOAT", "float8": "DOUBLE", "real": "FLOAT",
    "double precision": "DOUBLE", "double": "DOUBLE", "dec": "DECIMAL", "fixed": "DECIMAL",
    "binary_float": "FLOAT", "binary_double": "DOUBLE",

    # Character & Text Types
    "char": "STRING", "varchar": "STRING", "character varying": "STRING", "character": "STRING",
    "varchar2": "STRING", "nvarchar2": "STRING", "clob": "STRING", "nclob": "STRING",
    "text": "STRING", "tinytext": "STRING", "mediumtext": "STRING", "longtext": "STRING",
    "nchar": "STRING", "nvarchar": "STRING", "ntext": "STRING", "long": "STRING",
    "citext": "STRING", "name": "STRING", "bpchar": "STRING",
    "enum": "STRING", "set": "STRING", "rowid": "STRING", "urowid": "STRING",

    # Binary types
    "blob": "BINARY", "tinyblob": "BINARY", "mediumblob": "BINARY", "longblob": "BINARY",
    "binary": "BINARY", "varbinary": "BINARY", "image": "BINARY",
    "raw": "BINARY", "long raw": "BINARY", "bfile": "STRING",
    "bytea": "BINARY", "timestamp": "TIMESTAMP", "rowversion": "BINARY",

    # Date & Time types
    "date": "TIMESTAMP", "datetime": "TIMESTAMP", "datetime2": "TIMESTAMP", "smalldatetime": "TIMESTAMP",
    "timestamptz": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with local time zone": "TIMESTAMP",
    "time": "STRING", "timetz": "STRING", "time with time zone": "STRING",
    "time without time zone": "STRING", "interval": "STRING",

    # Specialized / Semi-structured types
    "uniqueidentifier": "STRING", "uuid": "STRING",
    "json": "STRING", "jsonb": "STRING", "xmltype": "STRING",
    "xml": "STRING", "datetimeoffset": "STRING", "sysname": "STRING", "hierarchyid": "STRING",
    "inet": "STRING", "cidr": "STRING", "macaddr": "STRING", "macaddr8": "STRING",
    "tsvector": "STRING", "tsquery": "STRING",
    "geometry": "STRING", "point": "STRING", "linestring": "STRING", "polygon": "STRING",
    "multipoint": "STRING", "multilinestring": "STRING", "multipolygon": "STRING",
    "geometrycollection": "STRING", "lseg": "STRING", "box": "STRING", "path": "STRING", "circle": "STRING"
}

def map_oracle_type(name: str, precision: int | None = None, scale: int | None = None) -> str:
    raw = name.lower().strip().replace('"', '')
    # Check for NUMBER with precision/scale
    declared = re.fullmatch(r"([a-z0-9_ ]+)\s*\(\s*(\*|\d+)\s*(?:,\s*(\*|-?\d+)\s*)?\)", raw)
    n = declared.group(1).strip() if declared else raw.split("(")[0].strip()

    if n in {"number", "numeric", "decimal", "dec"}:
        dec_p_str = declared.group(2) if declared else None
        dec_p = 38 if dec_p_str == "*" else (int(dec_p_str) if dec_p_str and dec_p_str.isdigit() else precision)
        dec_s_str = declared.group(3) if declared else None
        dec_s = int(dec_s_str) if dec_s_str and dec_s_str != "*" and (dec_s_str.isdigit() or (dec_s_str.startswith('-') and dec_s_str[1:].isdigit())) else scale

        if dec_p is not None and (dec_s is None or dec_s == 0):
            if dec_p <= 4:
                return "SMALLINT"
            elif dec_p <= 9:
                return "INT"
            elif dec_p <= 18:
                return "BIGINT"
            else:
                return f"DECIMAL({dec_p},0)"
        elif dec_p is not None and dec_s is not None and dec_s > 0:
            return f"DECIMAL({dec_p},{dec_s})"
        elif dec_p is None and dec_s is None:
            # Unconstrained NUMBER in Oracle often holds floating-point or arbitrary precision
            return "DECIMAL(38,10)"
        return f"DECIMAL({dec_p or 38},{dec_s or 0})"

    if n in {"binary_float", "float"}:
        return "FLOAT"
    if n in {"binary_double", "double precision"}:
        return "DOUBLE"
    if n in {"date"}:
        # Oracle DATE includes time (HH:MI:SS), so TIMESTAMP preserves data fidelity
        return "TIMESTAMP"
    if n in {"timestamp", "timestamp with time zone", "timestamp with local time zone"}:
        return "TIMESTAMP"
    if n in {"raw", "long raw", "blob"}:
        return "BINARY"
    if n in {"clob", "nclob", "long", "varchar2", "nvarchar2", "char", "nchar", "rowid", "urowid", "xmltype", "bfile"}:
        return "STRING"

    return TYPE_MAP.get(n, "STRING")

def map_mysql_type(name: str, precision: int | None = None, scale: int | None = None) -> str:
    raw = name.lower().strip().replace('`', '').replace('"', '')
    # Check for tinyint(1) boolean pattern in MySQL
    if re.search(r"\btinyint\s*\(\s*1\s*\)", raw):
        return "BOOLEAN"
    # Unsigned handling
    is_unsigned = "unsigned" in raw
    clean = re.sub(r"\s+unsigned\b", "", raw)
    clean = re.sub(r"\s+zerofill\b", "", clean)
    
    if is_unsigned:
        if clean.startswith("bigint"):
            return "DECIMAL(20,0)"
        elif clean.startswith("int") or clean.startswith("integer") or clean.startswith("mediumint"):
            return "BIGINT"
        elif clean.startswith("smallint"):
            return "INT"
        elif clean.startswith("tinyint"):
            return "SMALLINT"

    # Enums & Sets
    if clean.startswith("enum") or clean.startswith("set"):
        return "STRING"

    # Bit types: bit(1) -> BOOLEAN, bit(>1) -> BINARY
    if clean.startswith("bit"):
        m = re.search(r"bit\s*\(\s*(\d+)\s*\)", clean)
        if m and m.group(1) == "1":
            return "BOOLEAN"
        elif m and int(m.group(1)) > 1:
            return "BINARY"
        return "BOOLEAN"

    declared = re.fullmatch(r"([a-z0-9_ ]+)\s*\(\s*(max|\d+)\s*(?:,\s*(\d+)\s*)?\)", clean)
    n = declared.group(1).strip() if declared else clean.split("(")[0].strip()

    if n in {"decimal", "numeric", "dec", "fixed", "number"}:
        declared_precision = int(declared.group(2)) if declared and declared.group(2).isdigit() else None
        declared_scale = int(declared.group(3)) if declared and declared.group(3) else None
        p = precision or declared_precision or 38
        s = scale if scale is not None else (declared_scale or 0)
        return f"DECIMAL({p},{s})"
    if n in {"year"}:
        return "INT"
    if n in {"time"}:
        return "STRING"
    return TYPE_MAP.get(n, "STRING")

def map_postgres_type(name: str, precision: int | None = None, scale: int | None = None) -> str:
    raw = name.lower().strip().replace('"', '')
    if raw.endswith("[]") or (raw.startswith("_") and len(raw) > 1):
        base = raw[:-2] if raw.endswith("[]") else raw[1:]
        base_mapped = map_postgres_type(base)
        return f"ARRAY<{base_mapped}>"
    raw = raw.replace('[', '').replace(']', '')
    declared = re.fullmatch(r"([a-z0-9_ ]+)\s*\(\s*(max|\d+)\s*(?:,\s*(\d+)\s*)?\)", raw)
    n = declared.group(1).strip() if declared else raw
    if n in {"decimal", "numeric", "number"}:
        declared_precision = int(declared.group(2)) if declared and declared.group(2).isdigit() else None
        declared_scale = int(declared.group(3)) if declared and declared.group(3) else None
        return f"DECIMAL({precision or declared_precision or 38},{scale if scale is not None else (declared_scale or 0)})"
    if n in {"money", "smallmoney"}:
        return "DECIMAL(19,4)"
    if n in {"time", "timetz", "time with time zone", "time without time zone", "interval"}:
        return "STRING"
    if n in {"sql_variant", "geography", "geometry"}:
        return "STRING"
    return TYPE_MAP.get(n, "STRING")

def map_sqlserver_type(name: str, precision: int | None = None, scale: int | None = None) -> str:
    raw = name.lower().strip().replace('"', '').replace('[', '').replace(']', '')
    if raw in {"timestamp", "rowversion"}:
        return "BINARY"
    return map_mysql_type(name, precision, scale)

def map_source_type(name: str, precision: int | None = None, scale: int | None = None, source_type: str = "ORACLE") -> str:
    st = (source_type or "ORACLE").upper()
    if st == "ORACLE":
        return map_oracle_type(name, precision, scale)
    elif st == "MYSQL":
        return map_mysql_type(name, precision, scale)
    elif st in {"POSTGRESQL", "POSTGRES"}:
        return map_postgres_type(name, precision, scale)
    return map_sqlserver_type(name, precision, scale)

def classify_layer(object_type: str, definition: str|None="", name: str="") -> tuple[str,float,str]:
    t = object_type.upper(); d=(definition or "").lower(); n=name.lower()
    score_gold = 0
    if any(k in d for k in ["group by","sum(","avg(","count(","rollup","cube"]): score_gold += 2
    if any(k in n for k in ["fact","dim","aggregate","agg_","kpi","report","semantic"]): score_gold += 2
    if t in {"VIEW","PROCEDURE","FUNCTION"} and any(k in d for k in ["join ","case ","row_number(","dense_rank("]):
        if score_gold < 2: return "SILVER",0.82,"Reusable transformation/business logic detected"
    if score_gold >= 3: return "GOLD",0.86,"Reporting/dimensional/aggregation signals detected"
    if t == "TABLE": return "BRONZE",0.80,"Source-aligned persistent table candidate"
    if t in {"TRIGGER"}: return "SILVER",0.55,"Trigger requires architectural review; Silver is only a planning placeholder"
    return "SILVER",0.70,"Reusable non-table transformation object"

def classify_procedure(definition: str) -> tuple[str,str]:
    d=definition.lower()
    if "cursor" in d: return "MANUAL_REVIEW","Notebook or Workflow redesign"
    if "begin tran" in d or "transaction" in d: return "OPERATIONAL_TRANSACTION","Manual redesign"
    if "merge " in d or any(k in d for k in ["insert ","update ","delete "]): return "ETL_LOAD","Databricks SQL or PySpark/Lakeflow"
    if any(k in d for k in ["select ","group by","having"]): return "REPORTING_QUERY","Databricks SQL"
    return "UNSUPPORTED","Manual review"

def classify_function(definition: str) -> tuple[str,str]:
    d=definition.lower()
    if "returns table" in d and "begin" not in d: return "INLINE_TVF","VIEW_OR_SQL_FUNCTION"
    if "returns @" in d or ("returns table" in d and "begin" in d): return "MULTI_STATEMENT_TVF","PYSPARK_OR_TRANSFORMATION"
    return "SCALAR_UDF","SQL_FUNCTION_OR_EXPRESSION"

def classify_trigger(definition: str) -> tuple[str,str]:
    d=definition.lower()
    if "audit" in d or "history" in d: return "AUDIT","CDF or audit pipeline"
    if "raiserror" in d or "throw" in d: return "VALIDATION","Constraint/expectation/application logic"
    if any(k in d for k in ["send","mail","notify"]): return "NOTIFICATION","Workflow/notification integration"
    return "OPERATIONAL_SIDE_EFFECT","ARCHITECT_REVIEW"

TOKEN_PATTERN = re.compile(
    r'(?P<comment>--[^\r\n]*|/\*[\s\S]*?\*/)|'
    r'(?P<string>(?:N|n)?\'(?:[^\']|\'\')*\')|'
    r'(?P<plus>\+)|'
    r'(?P<boundary>[,;])|'
    r'(?P<paren>[()])|'
    r'(?P<ws>\s+)|'
    r'(?P<word>[A-Za-z0-9_#@$]+|\[[^\]]+\]|`[^`]+`)|'
    r'(?P<symbol>[^\sA-Za-z0-9_#@$\[\]`,\';()+]+)',
    re.DOTALL,
)

STRING_FUNC_NAMES = {"CHAR", "CHR", "SPACE", "REPLICATE", "STR", "CONCAT", "CONCAT_WS"}


def rewrite_tsql_concat(sql: str) -> str:
    """Rewrite SQL Server string concatenation operator '+' to Databricks '||'."""
    if not sql or "+" not in sql:
        return sql

    tokens = []
    for m in TOKEN_PATTERN.finditer(sql):
        tokens.append({"kind": m.lastgroup, "val": m.group(0)})

    def is_ignorable(idx: int) -> bool:
        if idx < 0 or idx >= len(tokens):
            return True
        return tokens[idx]["kind"] in ("comment", "ws")

    def prev_sig(idx: int) -> int | None:
        p = idx - 1
        while p >= 0 and is_ignorable(p):
            p -= 1
        return p if p >= 0 else None

    def next_sig(idx: int) -> int | None:
        n = idx + 1
        while n < len(tokens) and is_ignorable(n):
            n += 1
        return n if n < len(tokens) else None

    string_like: set[int] = set()
    for i, t in enumerate(tokens):
        if t["kind"] == "string":
            string_like.add(i)

    matching_open: dict[int, int] = {}
    matching_close: dict[int, int] = {}
    paren_stack: list[int] = []
    for i, t in enumerate(tokens):
        if t["kind"] == "paren":
            if t["val"] == "(":
                paren_stack.append(i)
            elif t["val"] == ")" and paren_stack:
                open_i = paren_stack.pop()
                matching_open[i] = open_i
                matching_close[open_i] = i
                p = prev_sig(open_i)
                if p is not None and tokens[p]["kind"] == "word":
                    if tokens[p]["val"].upper() in STRING_FUNC_NAMES:
                        string_like.add(p)
                        string_like.add(i)

    changed = True
    while changed:
        changed = False
        for i, t in enumerate(tokens):
            if t["kind"] == "plus":
                p = prev_sig(i)
                n = next_sig(i)
                if p is not None and n is not None:
                    if tokens[p]["kind"] == "boundary" or tokens[n]["kind"] == "boundary":
                        continue
                    if p in string_like or n in string_like:
                        t["kind"] = "concat"
                        t["val"] = "||"
                        string_like.add(p)
                        string_like.add(n)
                        if tokens[p]["val"] == ")" and p in matching_open:
                            open_i = matching_open[p]
                            string_like.add(open_i)
                            func_p = prev_sig(open_i)
                            if func_p is not None:
                                string_like.add(func_p)
                        if tokens[n]["kind"] == "word":
                            next_p = next_sig(n)
                            if next_p is not None and tokens[next_p]["val"] == "(" and next_p in matching_close:
                                string_like.add(matching_close[next_p])
                        changed = True

    return "".join(t["val"] for t in tokens)


def rewrite_recursive_cte(sql: str) -> str:
    """Rewrite self-referencing SQL Server CTEs to standard Databricks 'WITH RECURSIVE'."""
    if not sql or "WITH" not in sql.upper():
        return sql

    pattern = re.compile(r"\bWITH\s+(?!RECURSIVE\b)", re.I)

    for match in list(pattern.finditer(sql))[::-1]:
        with_start = match.start()
        with_end = match.end()
        remainder = sql[with_end:]

        cte_def_pattern = re.compile(
            r"([`\[\w]+)\s*(?:\([^)]*\))?\s*AS\s*\(",
            re.I,
        )

        is_recursive = False
        pos = 0
        while pos < len(remainder):
            cte_m = cte_def_pattern.search(remainder, pos)
            if not cte_m:
                break
            cte_name = cte_m.group(1).strip("`[]")
            body_start = cte_m.end()

            depth = 1
            i = body_start
            in_single_line_comment = False
            in_multi_line_comment = False
            in_string = False

            while depth > 0 and i < len(remainder):
                ch = remainder[i]
                next_ch = remainder[i + 1] if i + 1 < len(remainder) else ""

                if in_single_line_comment:
                    if ch == "\n":
                        in_single_line_comment = False
                elif in_multi_line_comment:
                    if ch == "*" and next_ch == "/":
                        in_multi_line_comment = False
                        i += 1
                elif in_string:
                    if ch == "'":
                        if next_ch == "'":
                            i += 1
                        else:
                            in_string = False
                else:
                    if ch == "-" and next_ch == "-":
                        in_single_line_comment = True
                        i += 1
                    elif ch == "/" and next_ch == "*":
                        in_multi_line_comment = True
                        i += 1
                    elif ch == "'":
                        in_string = True
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1

                i += 1

            cte_body = remainder[body_start : i - 1]
            if re.search(rf"\b{re.escape(cte_name)}\b", cte_body, re.I):
                is_recursive = True
                break
            pos = i

        if is_recursive:
            sql = sql[:with_start] + "WITH RECURSIVE " + sql[with_end:]

    return sql


def rewrite_common_tsql(sql: str) -> str:
    out = rewrite_tsql_concat(sql)
    out = rewrite_recursive_cte(out)
    out = re.sub(r"\b(?:GETDATE|SYSDATE|SYSTIMESTAMP)\s*(?:\(\s*\))?", "current_timestamp()", out, flags=re.I)
    out = re.sub(r"\b(?:ISNULL|NVL)\s*\(", "coalesce(", out, flags=re.I)
    out = re.sub(r"\[([^\]]+)\]", r"`\1`", out)
    out = re.sub(r"\bTOP\s*\(?(\d+)\)?\s+", "", out, flags=re.I)
    return out


def rewrite_common_postgres(sql: str) -> str:
    """Rewrite PostgreSQL specific SQL syntax to standard Databricks SQL."""
    if not sql:
        return sql
    out = rewrite_recursive_cte(sql)
    # Convert now() / current_timestamp to Databricks current_timestamp()
    out = re.sub(r"\bNOW\s*\(\s*\)", "current_timestamp()", out, flags=re.I)
    # Double quoted identifiers "my_col" -> `my_col`
    out = re.sub(r'"([^"]+)"', r"`\1`", out)
    # Square bracket identifiers [my_col] -> `my_col`
    out = re.sub(r"\[([^\]]+)\]", r"`\1`", out)
    # Convert PostgreSQL string_agg(col, ',') -> array_join(collect_list(col), ',') or concat_ws
    # Convert common PostgreSQL casts like ::text, ::int, ::timestamp
    out = re.sub(r"::text\b", "", out, flags=re.I)
    out = re.sub(r"::varchar\b", "", out, flags=re.I)
    out = re.sub(r"::jsonb?\b", "", out, flags=re.I)
    out = re.sub(r"::int(?:eger|4)?\b", "", out, flags=re.I)
    out = re.sub(r"::bigint\b", "", out, flags=re.I)
    out = re.sub(r"::timestamp\b", "", out, flags=re.I)
    out = re.sub(r"::date\b", "", out, flags=re.I)
    out = re.sub(r"::boolean?\b", "", out, flags=re.I)
    # ILIKE is supported in modern Databricks, but LIKE LOWER(...) is also safe
    return out


def rewrite_common_oracle(sql: str) -> str:
    """Rewrite Oracle specific SQL / PL/SQL syntax to standard Databricks SQL."""
    if not sql:
        return sql
    out = rewrite_recursive_cte(sql)
    # Remove DBMS_OUTPUT logging statements (PUT_LINE, PUT, NEW_LINE, ENABLE, DISABLE)
    out = re.sub(r"(?is)\bDBMS_OUTPUT\.[A-Za-z0-9_]+\s*(?:\([^)]*\))?\s*;?", "", out)
    # Remove other unsupported Oracle DBMS / UTL package calls
    out = re.sub(r"(?is)\b(?:DBMS_LOCK|DBMS_UTILITY|UTL_FILE|UTL_HTTP)\.[A-Za-z0-9_]+\s*(?:\([^)]*\))?\s*;?", "", out)
    # SYSDATE, SYSTIMESTAMP -> current_timestamp()
    out = re.sub(r"\b(?:SYSDATE|SYSTIMESTAMP)\b", "current_timestamp()", out, flags=re.I)
    # TRUNC(current_timestamp()) or TRUNC(date) -> current_date()
    out = re.sub(r"\bTRUNC\s*\(\s*current_timestamp\(\)\s*\)", "current_date()", out, flags=re.I)
    out = re.sub(r"\bTRUNC\s*\(\s*SYSDATE\s*\)", "current_date()", out, flags=re.I)
    # NVL(a, b) -> coalesce(a, b)
    out = re.sub(r"\bNVL\s*\(", "coalesce(", out, flags=re.I)
    # NVL2(a, b, c) -> CASE WHEN a IS NOT NULL THEN b ELSE c END
    out = re.sub(
        r"\bNVL2\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^)]+)\s*\)",
        r"CASE WHEN \1 IS NOT NULL THEN \2 ELSE \3 END",
        out,
        flags=re.I,
    )
    # Double quoted identifiers "MY_COL" -> `MY_COL`
    out = re.sub(r'"([^"]+)"', r"`\1`", out)
    # Standalone NULL statement in PL/SQL
    out = re.sub(r"(?im)^\s*NULL\s*;\s*$", "", out)
    return out


def rewrite_source_sql(sql: str, source_type: str = "ORACLE") -> str:
    st = (source_type or "ORACLE").upper()
    if st == "ORACLE":
        return rewrite_common_oracle(sql)
    elif st in {"POSTGRESQL", "POSTGRES"}:
        return rewrite_common_postgres(sql)
    return rewrite_common_tsql(sql)


