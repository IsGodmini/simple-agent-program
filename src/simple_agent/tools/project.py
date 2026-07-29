"""Tools for querying the persistent incremental project source index."""

import json
from dataclasses import asdict
from typing import Any, Dict

from ..project_index import ProjectIndex, project_hit_to_dict
from .base import Tool


class ProjectOverviewTool(Tool):
    name = "project_overview"
    description = (
        "读取工作区共享的持久化项目地图，包括文件树、技术栈、清单、入口、"
        "符号和索引状态。优先使用它理解项目；不会重新读取未变化的源码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "返回文件树的最大深度，默认3。",
            },
            "max_entries": {
                "type": "integer",
                "minimum": 20,
                "maximum": 1000,
                "description": "文件树最多返回条目数，默认300。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        max_depth = arguments.get("max_depth", 3)
        max_entries = arguments.get("max_entries", 300)
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 10:
            raise ValueError("max_depth must be from 1 to 10")
        if not isinstance(max_entries, int) or not 20 <= max_entries <= 1000:
            raise ValueError("max_entries must be from 20 to 1000")
        return json.dumps(
            self.project_index.overview(max_depth, max_entries),
            ensure_ascii=False,
            indent=2,
        )


class QueryProjectIndexTool(Tool):
    name = "query_project_index"
    description = (
        "使用持久化全文索引检索与需求相关的代码片段，不扫描全部项目文件。"
        "返回路径和精确行范围；修改前仍需用 read_file 读取当前文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "功能、错误、业务概念、符号或代码关键词。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "最多返回片段数，默认8。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        limit = arguments.get("limit", 8)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be from 1 to 20")
        hits = self.project_index.search(query, limit)
        if not hits:
            return "持久化项目索引中没有找到相关代码。"
        return json.dumps(
            [project_hit_to_dict(hit) for hit in hits],
            ensure_ascii=False,
            indent=2,
        )


class SearchSymbolsTool(Tool):
    name = "search_symbols"
    description = (
        "在持久化项目索引中搜索类、函数、接口和其他声明，返回文件、行号及"
        "签名；适合在读取文件前定位实现。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "符号名或其一部分。"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "最多返回数量，默认30。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        limit = arguments.get("limit", 30)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        symbols = self.project_index.search_symbols(query, limit)
        if not symbols:
            return "没有找到匹配的项目符号。"
        return json.dumps(
            [asdict(symbol) for symbol in symbols],
            ensure_ascii=False,
            indent=2,
        )


class FindReferencesTool(Tool):
    name = "find_references"
    description = (
        "从持久化代码片段中查找一个符号的引用位置，不重新扫描磁盘源码。"
        "返回的行用于导航，修改前应使用 read_file 核对当前内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "完整符号名称。"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "最多返回位置数，默认50。",
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        symbol = arguments.get("symbol")
        limit = arguments.get("limit", 50)
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        references = self.project_index.find_references(symbol, limit)
        if not references:
            return "没有找到该符号的引用。"
        return json.dumps(references, ensure_ascii=False, indent=2)


class DependencyGraphTool(Tool):
    name = "dependency_graph"
    description = (
        "读取持久化索引中的 import/use/require 关系，快速了解一个文件或整个"
        "项目的依赖边，不重新读取源码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "可选项目相对文件路径；省略返回全项目依赖。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "最多返回依赖边数，默认100。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        path = arguments.get("path")
        limit = arguments.get("limit", 100)
        if path is not None and (
            not isinstance(path, str) or not path.strip()
        ):
            raise ValueError("path must be a non-empty string")
        imports = self.project_index.list_imports(path, limit)
        if not imports:
            return "索引中没有找到匹配的依赖关系。"
        return json.dumps(imports, ensure_ascii=False, indent=2)


class IndexStatusTool(Tool):
    name = "index_status"
    description = (
        "查看项目索引是否就绪、最近刷新时间，以及文件、代码片段、符号和"
        "依赖数量，不读取源码。"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        return json.dumps(
            self.project_index.status(),
            ensure_ascii=False,
            indent=2,
        )


class RefreshProjectIndexTool(Tool):
    name = "refresh_project_index"
    description = (
        "增量刷新指定文件或整个持久化项目索引。正常需求开始时系统会自动"
        "刷新；仅在外部工具刚修改代码而索引尚未更新时调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
                "description": "可选项目相对路径；省略表示检查整个工作区。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, project_index: ProjectIndex) -> None:
        self.project_index = project_index

    def execute(self, arguments: Dict[str, Any]) -> str:
        paths = arguments.get("paths")
        if paths is not None and (
            not isinstance(paths, list)
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            raise ValueError("paths must be an array of non-empty strings")
        result = self.project_index.refresh(paths=paths)
        return json.dumps(asdict(result), ensure_ascii=False, indent=2)
