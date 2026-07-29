"""Local HTTP API and web client for Simple Agent."""

import argparse
import json
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import AgentResult
from .cli import build_agent
from .knowledge import (
    MAX_SOURCE_BYTES,
    KnowledgeBase,
    document_to_dict,
)
from .memory import ProjectMemoryStore
from .session import SessionManager
from .workspace import Workspace

WEB_ROOT = Path(__file__).parent / "web"


class SessionCreateRequest(BaseModel):
    workspace: str
    title: str = Field(default="", max_length=200)


class RequirementCreateRequest(BaseModel):
    workspace: str
    session_id: str
    request: str = Field(min_length=1, max_length=100_000)
    agent_mode: str = "auto"


@dataclass
class JobRecord:
    job_id: str
    workspace: str
    session_id: str
    request: str
    agent_mode: str
    status: str = "queued"
    phase: str = "queued"
    progress: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: str = ""

AgentFactory = Callable[..., Any]


class JobManager:
    """Run requirements in background threads and serialize each workspace."""

    def __init__(
        self,
        agent_factory: AgentFactory = build_agent,
        max_workers: int = 4,
    ) -> None:
        self.agent_factory = agent_factory
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="simple-agent-web",
        )
        self._jobs: Dict[str, JobRecord] = {}
        self._workspace_locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def submit(self, request: RequirementCreateRequest) -> Dict[str, Any]:
        workspace = _workspace(request.workspace)
        store = ProjectMemoryStore(workspace)
        store.get_session(request.session_id)
        if request.agent_mode not in {"auto", "react", "plan"}:
            raise ValueError("agent_mode must be auto, react, or plan")
        job = JobRecord(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            workspace=str(workspace.root),
            session_id=request.session_id,
            request=request.request.strip(),
            agent_mode=request.agent_mode,
        )
        with self._lock:
            self._jobs[job.job_id] = job
            workspace_lock = self._workspace_locks.setdefault(
                str(workspace.root),
                threading.Lock(),
            )
        self._record_progress(
            job.job_id,
            {
                "event": "queued",
                "message": "需求已进入当前工作区执行队列",
            },
        )
        self.executor.submit(self._run, job.job_id, workspace_lock)
        return self.get(job.job_id)

    def get(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"job does not exist: {job_id}")
            return asdict(job)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job_id: str, workspace_lock: threading.Lock) -> None:
        try:
            with workspace_lock:
                self._update(
                    job_id,
                    status="running",
                    phase="context_building",
                )
                self._record_progress(
                    job_id,
                    {
                        "event": "context_building",
                        "message": (
                            "正在构建会话摘要、场景记忆与知识库上下文"
                        ),
                    },
                )
                snapshot = self.get(job_id)
                workspace = _workspace(snapshot["workspace"])
                store = ProjectMemoryStore(workspace)
                knowledge = KnowledgeBase(workspace)
                session_manager = SessionManager(
                    store,
                    knowledge_base=knowledge,
                    session_id=snapshot["session_id"],
                )
                requirement = session_manager.start_requirement(
                    snapshot["request"]
                )
                self._record_progress(
                    job_id,
                    {
                        "event": "context_ready",
                        "memory_count": len(requirement.memory_summary_ids),
                        "knowledge_count": len(
                            requirement.knowledge_citations
                        ),
                        "message": (
                            "上下文构建完成："
                            f"{len(requirement.memory_summary_ids)} 条记忆，"
                            f"{len(requirement.knowledge_citations)} 条知识引用"
                        ),
                    },
                )
                try:
                    result: AgentResult = self.agent_factory(
                        workspace.root,
                        memory_store=store,
                        knowledge_base=knowledge,
                        agent_mode=snapshot["agent_mode"],
                        progress_callback=lambda event: self._record_progress(
                            job_id,
                            event,
                        ),
                    ).run(
                        snapshot["request"],
                        context_messages=requirement.context_messages,
                    )
                except Exception as exc:
                    session_manager.fail_task(requirement, exc)
                    raise
                self._record_progress(
                    job_id,
                    {
                        "event": "memory_writing",
                        "message": "正在写入需求摘要与场景记忆",
                    },
                )
                summary = session_manager.complete_task(requirement, result)
                payload = {
                    "requirement_id": requirement.task_id,
                    "session_id": requirement.session_id,
                    "content": result.content,
                    "summary": asdict(summary),
                    "workflow": result.workflow,
                    "iterations": result.iterations,
                    "compactions": result.compactions,
                }
            self._record_progress(
                job_id,
                {
                    "event": "completed",
                    "message": "需求结果与项目记忆均已保存",
                },
            )
            self._update(
                job_id,
                status="completed",
                phase="completed",
                result=payload,
            )
        except Exception as exc:
            self._record_progress(
                job_id,
                {
                    "event": "failed",
                    "message": f"执行失败：{type(exc).__name__}",
                },
            )
            self._update(
                job_id,
                status="failed",
                phase="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in changes.items():
                setattr(job, name, value)

    def _record_progress(
        self,
        job_id: str,
        event: Dict[str, Any],
    ) -> None:
        record = {
            **event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            job = self._jobs[job_id]
            job.phase = str(record.get("event") or job.phase)
            job.progress.append(record)
            if len(job.progress) > 200:
                job.progress = job.progress[-200:]


def create_app(
    default_workspace: Optional[Path] = None,
    agent_factory: AgentFactory = build_agent,
) -> FastAPI:
    """Create the local API; dependencies can be replaced in tests."""
    default_root = (default_workspace or Path.cwd()).expanduser().resolve()
    jobs = JobManager(agent_factory=agent_factory)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(
        title="Simple Agent Client",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.jobs = jobs
    app.state.default_workspace = str(default_root)
    app.mount(
        "/assets",
        StaticFiles(directory=str(WEB_ROOT)),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/api/bootstrap")
    def bootstrap() -> Dict[str, Any]:
        return {
            "default_workspace": app.state.default_workspace,
            "agent_modes": ["auto", "react", "plan"],
            "max_upload_bytes": MAX_SOURCE_BYTES,
        }

    @app.get("/api/workspace")
    def workspace_overview(
        path: str = Query(..., min_length=1),
    ) -> Dict[str, Any]:
        try:
            workspace = _workspace(path)
            store = ProjectMemoryStore(workspace)
            if not store.list_sessions():
                store.ensure_session("default")
            knowledge = KnowledgeBase(workspace)
            return {
                "path": str(workspace.root),
                "sessions": [
                    asdict(session) for session in store.list_sessions()
                ],
                "knowledge": [
                    document_to_dict(document)
                    for document in knowledge.list_documents()
                ],
            }
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions")
    def list_sessions(
        workspace: str = Query(..., min_length=1),
    ) -> List[Dict[str, Any]]:
        try:
            store = ProjectMemoryStore(_workspace(workspace))
            return [asdict(session) for session in store.list_sessions()]
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions", status_code=201)
    def create_session(request: SessionCreateRequest) -> Dict[str, Any]:
        try:
            store = ProjectMemoryStore(_workspace(request.workspace))
            return asdict(store.create_session(request.title))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/requirements")
    def list_requirements(
        session_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> List[Dict[str, Any]]:
        try:
            store = ProjectMemoryStore(_workspace(workspace))
            store.get_session(session_id)
            return [
                asdict(summary)
                for summary in store.list_summaries()
                if summary.session_id == session_id
            ]
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/episodes/{requirement_id}")
    def read_episode(
        requirement_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> Dict[str, Any]:
        try:
            return ProjectMemoryStore(
                _workspace(workspace)
            ).read_episode(requirement_id)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/requirements/{requirement_id}")
    def read_requirement_result(
        requirement_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> Dict[str, Any]:
        try:
            episode = ProjectMemoryStore(
                _workspace(workspace)
            ).read_episode(requirement_id)
            return {
                "requirement_id": requirement_id,
                "status": episode.get("status", ""),
                "content": (
                    episode.get("final_content")
                    or episode.get("error")
                    or ""
                ),
                "workflow": episode.get("workflow"),
                "iterations": episode.get("iterations", 0),
                "compactions": episode.get("compactions", 0),
            }
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/requirements", status_code=202)
    def create_requirement(
        request: RequirementCreateRequest,
    ) -> Dict[str, Any]:
        try:
            return jobs.submit(request)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> Dict[str, Any]:
        try:
            return jobs.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/knowledge")
    def list_knowledge(
        workspace: str = Query(..., min_length=1),
    ) -> List[Dict[str, Any]]:
        try:
            return [
                document_to_dict(document)
                for document in KnowledgeBase(
                    _workspace(workspace)
                ).list_documents()
            ]
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/knowledge/upload", status_code=201)
    async def upload_knowledge(
        workspace: str = Query(..., min_length=1),
        files: List[UploadFile] = File(...),
    ) -> List[Dict[str, Any]]:
        try:
            knowledge = KnowledgeBase(_workspace(workspace))
            imported = []
            with tempfile.TemporaryDirectory(
                prefix="simple-agent-upload-"
            ) as directory:
                upload_root = Path(directory)
                for index, upload in enumerate(files):
                    name = Path(upload.filename or f"upload-{index}").name
                    content = await upload.read(MAX_SOURCE_BYTES + 1)
                    if len(content) > MAX_SOURCE_BYTES:
                        raise ValueError(
                            f"uploaded file exceeds {MAX_SOURCE_BYTES} bytes: "
                            f"{name}"
                        )
                    target = upload_root / f"{index}-{name}"
                    target.write_bytes(content)
                    imported.append(
                        knowledge.ingest(
                            target,
                            source_name=name,
                            source_identity=f"upload:{name}",
                        )
                    )
            return [document_to_dict(document) for document in imported]
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()

    @app.delete("/api/knowledge/{document_id}")
    def remove_knowledge(
        document_id: str,
        workspace: str = Query(..., min_length=1),
    ) -> Dict[str, Any]:
        try:
            removed = KnowledgeBase(_workspace(workspace)).remove(document_id)
            if not removed:
                raise HTTPException(
                    status_code=404,
                    detail=f"knowledge document does not exist: {document_id}",
                )
            return {"removed": True, "document_id": document_id}
        except HTTPException:
            raise
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def _workspace(path: str) -> Workspace:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("workspace path cannot be empty")
    return Workspace(Path(path.strip()))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start the local Simple Agent web client."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Initial workspace shown in the client.",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host",
        choices=["127.0.0.1", "localhost"],
        default="127.0.0.1",
        help="The client intentionally binds only to the local machine.",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        raise ValueError("port must be from 1 to 65535")
    uvicorn.run(
        create_app(default_workspace=args.workspace),
        host=args.host,
        port=args.port,
    )
