"""Connectors against real files, a real SQLite database and a real HTTP server."""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from gdap.connectors import get_registry
from gdap.core.contracts import ReadOptions, SourceSpec
from gdap.core.enums import SourceType
from gdap.core.errors import ConnectorError

pytestmark = pytest.mark.integration


def _source(name: str, connector: str, **config: object) -> SourceSpec:
    kind = (
        "sql" if connector.startswith("sql") else "rest" if connector.startswith("rest") else "file"
    )
    return SourceSpec(name=name, type=SourceType(kind), connector=connector, config=config)


def test_csv_connector_reads_and_infers_types(tmp_path: Path) -> None:
    path = tmp_path / "sales.csv"
    path.write_text("id,region,revenue,day\n1,North,100.5,2026-01-05\n2,South,,2026-01-06\n")
    connector = get_registry().create(_source("csv", "file.csv", path=str(path)))

    assert connector.test().ok
    schema = connector.infer_schema(ReadOptions())
    assert [column.dtype for column in schema.columns] == ["Int64", "String", "Float64", "Date"]

    frame = next(connector.read(ReadOptions()))
    assert frame.height == 2
    assert frame["revenue"].null_count() == 1


def test_csv_connector_streams_in_chunks(tmp_path: Path) -> None:
    path = tmp_path / "big.csv"
    path.write_text("n\n" + "\n".join(str(i) for i in range(5_000)) + "\n")
    connector = get_registry().create(_source("csv", "file.csv", path=str(path)))
    chunks = list(connector.read(ReadOptions(chunk_rows=1_000)))
    assert len(chunks) > 1
    assert sum(chunk.height for chunk in chunks) == 5_000


def test_gzip_and_directory_globbing(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("n\n1\n")
    with gzip.open(tmp_path / "b.csv.gz", "wt") as handle:
        handle.write("n\n2\n")
    connector = get_registry().create(_source("dir", "file.csv", path=str(tmp_path), pattern="*"))
    assert {obj.name for obj in connector.discover()} == {"a.csv", "b.csv.gz"}
    assert sum(chunk.height for chunk in connector.read(ReadOptions())) == 2


def test_json_and_xml_connectors(tmp_path: Path) -> None:
    (tmp_path / "nested.json").write_text(json.dumps({"data": [{"a": 1}, {"a": 2}]}))
    (tmp_path / "rows.xml").write_text(
        '<catalog><row id="1"><name>Widget</name></row><row id="2"><name>Gadget</name></row></catalog>'
    )
    json_connector = get_registry().create(
        _source("j", "file.json", path=str(tmp_path / "nested.json"), record_path="data")
    )
    assert next(json_connector.read(ReadOptions())).height == 2

    xml_connector = get_registry().create(_source("x", "file.xml", path=str(tmp_path / "rows.xml")))
    frame = next(xml_connector.read(ReadOptions()))
    assert frame.height == 2 and "name" in frame.columns


def test_missing_path_reports_a_useful_error(tmp_path: Path) -> None:
    connector = get_registry().create(
        _source("missing", "file.csv", path=str(tmp_path / "nope.csv"))
    )
    result = connector.test()
    assert not result.ok and "does not exist" in result.message


def test_sql_connector_discovers_and_reads_incrementally(tmp_path: Path) -> None:
    database = tmp_path / "demo.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (id INTEGER, name TEXT, ts TEXT)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [(1, "a", "2026-01-01"), (2, "b", "2026-02-01"), (3, "c", "2026-03-01")],
    )
    connection.commit()
    connection.close()

    connector = get_registry().create(
        _source("db", "sql", driver="sqlite", database=str(database), table="t")
    )
    objects = connector.discover()
    assert objects[0].name == "t"
    assert len(objects[0].schema_.columns) == 3  # type: ignore[union-attr]

    assert sum(chunk.height for chunk in connector.read(ReadOptions())) == 3
    incremental = sum(
        chunk.height
        for chunk in connector.read(ReadOptions(incremental_column="ts", since="2026-01-15"))
    )
    assert incremental == 2
    connector.close()


def test_sql_connector_refuses_unsafe_identifiers(tmp_path: Path) -> None:
    connector = get_registry().create(
        _source(
            "db", "sql", driver="sqlite", database=str(tmp_path / "x.db"), table="t; DROP TABLE t"
        )
    )
    with pytest.raises(ConnectorError, match="unsafe identifier"):
        list(connector.read(ReadOptions()))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        page = int(query.get("page", ["1"])[0])
        payload = {
            "results": [{"id": page * 10 + i, "value": i} for i in range(2)] if page <= 2 else []
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the test server
        return


@pytest.fixture
def http_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/items"
    server.shutdown()


def test_rest_connector_paginates(http_server: str) -> None:
    connector = get_registry().create(
        _source(
            "api",
            "rest",
            url=http_server,
            record_path="results",
            pagination={"type": "page", "page_size": 2, "max_pages": 5},
        )
    )
    assert connector.test().ok
    rows = sum(chunk.height for chunk in connector.read(ReadOptions()))
    assert rows == 4  # two pages of two, then an empty page stops the loop
    connector.close()


def test_registry_validates_required_config() -> None:
    from gdap.core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError, match="required config"):
        get_registry().create(SourceSpec(name="bad", type=SourceType.FILE, connector="file.csv"))
