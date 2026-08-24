"""Built-in connectors. Importing this module registers them in the global registry."""

from gdap.connectors.impl.file import CsvPlugin, FilePlugin, JsonPlugin, ParquetPlugin, XmlPlugin
from gdap.connectors.impl.memory import MemoryPlugin
from gdap.connectors.impl.rest import RestPlugin
from gdap.connectors.impl.sql import SqlPlugin
from gdap.connectors.registry import register_connector

for _plugin in (
    FilePlugin(),
    CsvPlugin(),
    JsonPlugin(),
    ParquetPlugin(),
    XmlPlugin(),
    SqlPlugin(),
    RestPlugin(),
    MemoryPlugin(),
):
    register_connector(_plugin, replace=True)

__all__ = [
    "CsvPlugin",
    "FilePlugin",
    "JsonPlugin",
    "MemoryPlugin",
    "ParquetPlugin",
    "RestPlugin",
    "SqlPlugin",
    "XmlPlugin",
]
