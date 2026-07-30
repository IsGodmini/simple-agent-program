"""Tools for querying the persistent project knowledge graph."""

import json
from dataclasses import asdict
from typing import Any, Dict

from ..project_graph import ProjectGraph, profile_to_dict
from .base import Tool


class ProjectGraphOverviewTool(Tool):
    name = "project_graph_overview"
    description = (
        "读取持久化项目知识图谱概览、关系类型和代表性文件功能。优先用它"
        "理解项目结构；它基于增量索引，不会重新读取未变化源码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "max_profiles": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "最多返回多少个代表性文件功能，默认30。",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        limit = arguments.get("max_profiles", 30)
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("max_profiles must be from 1 to 200")
        return json.dumps(
            self.project_graph.overview(limit),
            ensure_ascii=False,
            indent=2,
        )


class QueryFileProfilesTool(Tool):
    name = "query_file_profiles"
    description = (
        "按需求、业务概念、符号或职责搜索持久化文件功能说明。结果可用于先"
        "定位候选文件；修改前仍须用 read_file 核对当前源码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要查找的功能或概念。"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "最多返回数量，默认6。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        limit = arguments.get("limit", 6)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be from 1 to 30")
        profiles = self.project_graph.search_profiles(query, limit)
        if not profiles:
            return "项目图谱中没有找到匹配的文件功能说明。"
        return json.dumps(
            [profile_to_dict(profile) for profile in profiles],
            ensure_ascii=False,
            indent=2,
        )


class FileProfileTool(Tool):
    name = "file_profile"
    description = (
        "读取指定文件的持久化功能、职责、公开符号、依赖、关联测试和证据。"
        "这是导航摘要，不替代修改前读取当前文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "项目相对文件路径。"}
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        profile = self.project_graph.get_profile(path)
        if profile is None:
            return f"项目图谱中没有文件功能说明：{path}"
        return json.dumps(
            profile_to_dict(profile),
            ensure_ascii=False,
            indent=2,
        )


class QueryProjectGraphTool(Tool):
    name = "query_project_graph"
    description = (
        "查询一个文件在项目图谱中的依赖、被依赖、符号和测试关系，可递归"
        "查看最多4层；适合在读取源码前缩小影响范围。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "项目相对文件路径。"},
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": "关系深度，默认1。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "最多返回节点数，默认100。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        path = arguments.get("path")
        depth = arguments.get("depth", 1)
        limit = arguments.get("limit", 100)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ValueError("depth must be from 1 to 4")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        return json.dumps(
            self.project_graph.neighbors(path, depth, limit),
            ensure_ascii=False,
            indent=2,
        )


class ImpactAnalysisTool(Tool):
    name = "impact_analysis"
    description = (
        "基于项目图谱分析修改一个文件可能影响的文件、入向依赖和关联测试。"
        "适合编码前规划与编码后选择验证范围。"
    )
    parameters = QueryProjectGraphTool.parameters

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        path = arguments.get("path")
        depth = arguments.get("depth", 2)
        limit = arguments.get("limit", 100)
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not isinstance(depth, int) or not 1 <= depth <= 4:
            raise ValueError("depth must be from 1 to 4")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        return json.dumps(
            self.project_graph.impact_analysis(path, depth, limit),
            ensure_ascii=False,
            indent=2,
        )


class GraphStatusTool(Tool):
    name = "graph_status"
    description = (
        "查看 Neo4j 项目关系图、LLM 文件功能档案和 Chroma 向量检索状态。"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        return json.dumps(
            self.project_graph.status(),
            ensure_ascii=False,
            indent=2,
        )


class RefreshProjectGraphTool(Tool):
    name = "refresh_project_graph"
    description = (
        "增量刷新源码索引、变化文件的功能档案及项目关系，并按配置同步 "
        "Neo4j。通常系统会自动刷新，仅在外部修改后按需调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
                "description": "可选项目相对路径；省略表示检查整个工作区。",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, project_graph: ProjectGraph) -> None:
        self.project_graph = project_graph

    def execute(self, arguments: Dict[str, Any]) -> str:
        paths = arguments.get("paths")
        if paths is not None and (
            not isinstance(paths, list)
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            raise ValueError("paths must be an array of non-empty strings")
        return json.dumps(
            asdict(self.project_graph.refresh(paths)),
            ensure_ascii=False,
            indent=2,
        )
