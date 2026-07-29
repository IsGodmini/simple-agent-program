"""Tools for selectively retrieving user-provided project knowledge."""

import json
from typing import Any, Dict

from ..knowledge import KnowledgeBase, document_to_dict, hit_to_dict
from .base import Tool


class SearchKnowledgeTool(Tool):
    """Retrieve relevant uploaded project guidelines and reference documents."""

    name = "search_knowledge"
    description = (
        "搜索用户上传的项目知识库，包括开发注意事项、项目规范、设计文档和"
        "其他参考资料。仅在当前需求可能受这些资料约束时调用；返回可引用的"
        "相关片段，而不是整份文档。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "当前问题、功能名、规范关键词或技术概念。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "最多返回片段数，默认5。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        hits = self.knowledge_base.search(query, limit)
        if not hits:
            return "没有找到相关的项目知识。"
        return json.dumps(
            [hit_to_dict(hit) for hit in hits],
            ensure_ascii=False,
            indent=2,
        )


class ReadKnowledgeTool(Tool):
    """Read one exact chunk after search returns its citation."""

    name = "read_knowledge"
    description = (
        "按 document_id 和 chunk_index 读取一个完整知识片段。仅在"
        "search_knowledge 返回的摘要不足时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "search_knowledge 返回的文档 ID。",
            },
            "chunk_index": {
                "type": "integer",
                "minimum": 1,
                "description": "片段序号。",
            },
        },
        "required": ["document_id", "chunk_index"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        max_chars: int = 30_000,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.max_chars = max_chars

    def execute(self, arguments: Dict[str, Any]) -> str:
        document_id = arguments.get("document_id")
        chunk_index = arguments.get("chunk_index")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document_id must be a non-empty string")
        if not isinstance(chunk_index, int) or chunk_index < 1:
            raise ValueError("chunk_index must be a positive integer")
        hit = hit_to_dict(
            self.knowledge_base.read_chunk(document_id, chunk_index)
        )
        content = json.dumps(hit, ensure_ascii=False, indent=2)
        if len(content) > self.max_chars:
            return content[: self.max_chars] + "\n...[知识片段已截断]"
        return content


class ListKnowledgeTool(Tool):
    """List indexed documents without loading their contents."""

    name = "list_knowledge"
    description = (
        "列出当前项目知识库中已上传的文档及其 ID、格式和片段数量，不读取正文。"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    def execute(self, arguments: Dict[str, Any]) -> str:
        documents = self.knowledge_base.list_documents()
        if not documents:
            return "项目知识库为空。"
        return json.dumps(
            [document_to_dict(document) for document in documents],
            ensure_ascii=False,
            indent=2,
        )
