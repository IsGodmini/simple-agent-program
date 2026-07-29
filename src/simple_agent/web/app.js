"use strict";

const state = {
  workspace: "",
  sessions: [],
  currentSession: "",
  requirements: [],
  knowledge: [],
  currentJob: null,
  polling: false,
};

const elements = {
  workspaceForm: document.querySelector("#workspace-form"),
  workspacePath: document.querySelector("#workspace-path"),
  connectionState: document.querySelector("#connection-state"),
  sessionList: document.querySelector("#session-list"),
  sessionTitle: document.querySelector("#session-title"),
  requirementCount: document.querySelector("#requirement-count"),
  transcript: document.querySelector("#transcript"),
  composer: document.querySelector("#composer"),
  requestInput: document.querySelector("#request-input"),
  sendButton: document.querySelector("#send-button"),
  newSessionButton: document.querySelector("#new-session-button"),
  sessionDialog: document.querySelector("#session-dialog"),
  sessionForm: document.querySelector("#session-form"),
  sessionName: document.querySelector("#session-name"),
  cancelSession: document.querySelector("#cancel-session"),
  uploadButton: document.querySelector("#upload-button"),
  knowledgeFiles: document.querySelector("#knowledge-files"),
  knowledgeList: document.querySelector("#knowledge-list"),
  runIndicator: document.querySelector("#run-indicator"),
  workflowView: document.querySelector("#workflow-view"),
  inspector: document.querySelector(".inspector"),
  memoryStatus: document.querySelector("#memory-status"),
  toast: document.querySelector("#toast"),
};

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message = payload && payload.detail ? payload.detail : payload;
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return payload;
}

function queryWorkspace() {
  return `workspace=${encodeURIComponent(state.workspace)}`;
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.className = `toast visible${isError ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.className = "toast";
  }, 3200);
}

function setConnected(connected) {
  elements.connectionState.lastChild.textContent = connected ? " 已连接" : " 未连接";
  elements.connectionState.classList.toggle("disconnected", !connected);
  elements.sendButton.disabled = !connected || state.polling;
  elements.newSessionButton.disabled = !connected;
  elements.uploadButton.disabled = !connected;
}

function formatTime(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function renderSessions() {
  elements.sessionList.replaceChildren();
  if (!state.sessions.length) {
    elements.sessionList.append(node("p", "empty-list", "还没有会话。"));
    return;
  }
  for (const session of state.sessions) {
    const button = node("button", "session-item");
    button.type = "button";
    if (session.session_id === state.currentSession) button.classList.add("active");
    const dot = node("span", "session-dot");
    const copy = node("span");
    copy.append(
      node("span", "session-name", session.title || session.session_id),
      node(
        "span",
        "session-meta",
        `${session.requirement_ids.length} 条需求 · ${formatTime(session.updated_at)}`,
      ),
    );
    button.append(dot, copy);
    button.addEventListener("click", () => selectSession(session.session_id));
    elements.sessionList.append(button);
  }
}

function renderKnowledge() {
  elements.knowledgeList.replaceChildren();
  if (!state.knowledge.length) {
    elements.knowledgeList.append(
      node("p", "empty-list", "暂无资料。上传规范、设计文档或开发注意事项。"),
    );
    return;
  }
  for (const document of state.knowledge) {
    const item = node("div", "knowledge-item");
    const extension = document.source_type.replace(".", "").slice(0, 4) || "file";
    const copy = node("div");
    copy.append(
      node("div", "knowledge-name", document.source_name),
      node("div", "knowledge-meta", `${document.chunk_count} 个片段`),
    );
    const remove = node("button", "remove-button", "×");
    remove.type = "button";
    remove.title = `删除 ${document.source_name}`;
    remove.setAttribute("aria-label", remove.title);
    remove.addEventListener("click", () => removeKnowledge(document));
    item.append(node("span", "file-type", extension), copy, remove);
    elements.knowledgeList.append(item);
  }
}

function appendResultMeta(container, requirement) {
  const meta = node("div", "result-meta");
  const status = node(
    "span",
    requirement.status === "completed" ? "verified" : "failed",
    requirement.status === "completed" ? "已完成" : "执行失败",
  );
  meta.append(status);
  if (requirement.verification) {
    const verification = node(
      "span",
      requirement.verification === "verified" ? "verified" : "",
      requirement.verification,
    );
    meta.append(verification);
  }
  if (requirement.files_changed && requirement.files_changed.length) {
    meta.append(node("span", "", `${requirement.files_changed.length} 个文件变更`));
  }
  if (requirement.finished_at) {
    meta.append(node("span", "", formatTime(requirement.finished_at)));
  }
  container.append(meta);
}

function markdownBlock(content) {
  const block = node("div", "markdown-body");
  block.innerHTML = window.SimpleMarkdown.render(content || "");
  return block;
}

function renderTranscript() {
  elements.transcript.replaceChildren();
  const current = state.sessions.find(
    (session) => session.session_id === state.currentSession,
  );
  elements.sessionTitle.textContent = current
    ? current.title
    : "请选择或创建会话";
  const extra = state.currentJob && ["queued", "running"].includes(state.currentJob.status)
    ? 1
    : 0;
  elements.requirementCount.textContent = `${state.requirements.length + extra} 条需求`;

  if (!state.requirements.length && !extra) {
    const empty = node("div", "empty-state");
    const orbit = node("span", "empty-orbit");
    orbit.append(node("i"));
    empty.append(
      orbit,
      node("span", "eyebrow", "BUILD WITH INTENT"),
      node("h2", "", "描述你想完成的项目"),
      node("p", "", "Agent 会理解代码、规划复杂任务、修改文件并根据测试结果反思。"),
    );
    elements.transcript.append(empty);
    return;
  }

  for (const requirement of state.requirements) {
    const block = node("article", "requirement");
    const user = node("div");
    user.append(
      node("div", "message-label", "YOUR REQUIREMENT"),
      node("div", "user-message", requirement.request),
    );
    const agent = node("div", "agent-message");
    agent.append(node("div", "message-label", "SIMPLE AGENT"));
    agent.append(
      markdownBlock(
        requirement.content || requirement.summary || "需求已记录。",
      ),
    );
    appendResultMeta(agent, requirement);
    block.append(user, agent);
    elements.transcript.append(block);
  }

  if (extra) {
    const block = node("article", "requirement");
    const user = node("div");
    user.append(
      node("div", "message-label", "YOUR REQUIREMENT"),
      node("div", "user-message", state.currentJob.request),
    );
    const agent = node("div", "agent-message");
    agent.append(node("div", "message-label", "SIMPLE AGENT"));
    const pending = node("span", "pending-line");
    const events = state.currentJob.progress || [];
    const latest = events.length ? events[events.length - 1].message : "";
    pending.append(node("i"), document.createTextNode(
      latest || (
        state.currentJob.status === "queued"
          ? "等待当前工作区任务…"
          : "正在理解项目并执行…"
      ),
    ));
    agent.append(pending);
    block.append(user, agent);
    elements.transcript.append(block);
  }
  elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

function workflowCard(title, label, text) {
  const card = node("section", "workflow-card");
  if (label) card.append(node("span", "tag", label));
  card.append(node("h3", "", title));
  if (text) card.append(node("p", "", text));
  return card;
}

function renderWorkflow(job) {
  elements.workflowView.replaceChildren();
  if (!job) {
    const empty = node("div", "inspector-empty");
    const bars = node("div", "signal-lines");
    bars.append(node("i"), node("i"), node("i"));
    empty.append(
      bars,
      node("p", "", "提交需求后，这里会呈现路由判断、执行计划和 Reflection 评审结果。"),
    );
    elements.workflowView.append(empty);
    elements.runIndicator.className = "run-indicator idle";
    elements.runIndicator.textContent = "空闲";
    return;
  }

  elements.inspector.classList.add("has-run");
  const active = ["queued", "running"].includes(job.status);
  elements.runIndicator.className = `run-indicator ${
    active ? "running" : job.status === "failed" ? "failed" : "idle"
  }`;
  elements.runIndicator.textContent = {
    queued: "排队中",
    running: "执行中",
    completed: "已完成",
    failed: "失败",
  }[job.status] || job.status;

  const progress = Array.isArray(job.progress) ? job.progress : [];
  if (progress.length) {
    const timelineCard = workflowCard("实时执行过程", "LIVE");
    const timeline = node("ol", "progress-timeline");
    const visibleEvents = progress.slice(-40);
    visibleEvents.forEach((event, index) => {
      const item = node("li", "progress-event");
      if (index === visibleEvents.length - 1 && active) {
        item.classList.add("current");
      }
      const marker = node("i");
      const copy = node("div");
      const iterationMeta = event.iteration
        ? `第 ${event.iteration} 轮`
        : "";
      const budgetMeta = event.requirement_maximum
        ? `总预算 ${event.requirement_used}/${event.requirement_maximum}`
        : "";
      copy.append(
        node("strong", "", event.message || event.event),
        node(
          "span",
          "",
          [
            event.role,
            event.tool,
            iterationMeta,
            budgetMeta,
            formatTime(event.timestamp),
          ]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      item.append(marker, copy);
      timeline.append(item);
    });
    timelineCard.append(timeline);
    elements.workflowView.append(timelineCard);
  }

  if (active) {
    return;
  }
  if (job.status === "failed") {
    elements.workflowView.append(workflowCard("执行未完成", "ERROR", job.error));
    return;
  }

  const workflow = job.result && job.result.workflow ? job.result.workflow : {};
  const mode = workflow.mode || job.agent_mode;
  const assessment = workflow.assessment || workflow.routing || {};
  elements.workflowView.append(
    workflowCard(
      "路由判断",
      mode,
      assessment.reason || assessment.rationale || `本次需求使用 ${mode} 模式完成。`,
    ),
  );

  const plan = workflow.plan || {};
  const steps = Array.isArray(plan.steps)
    ? plan.steps
    : Array.isArray(workflow.steps) ? workflow.steps : [];
  if (steps.length) {
    const card = workflowCard("执行计划", "PLAN");
    const list = node("ol", "plan-steps");
    for (const step of steps) {
      list.append(node("li", "", typeof step === "string"
        ? step
        : step.objective || step.title || step.description ||
          step.task || JSON.stringify(step)));
    }
    card.append(list);
    elements.workflowView.append(card);
  }

  const reviews = workflow.reviews || workflow.reflections || [];
  const normalizedReviews = Array.isArray(reviews) ? reviews : [reviews];
  for (const review of normalizedReviews.filter(Boolean)) {
    elements.workflowView.append(
      workflowCard(
        "Reflection 评审",
        review.verdict || review.status || "REVIEW",
        review.summary || review.reason || review.feedback || JSON.stringify(review),
      ),
    );
  }
  const iterationBudget = workflow.iteration_budget || {};
  const budgetText = iterationBudget.maximum
    ? ` · 需求总预算 ${iterationBudget.used}/${iterationBudget.maximum}`
    : "";
  elements.workflowView.append(
    workflowCard(
      "运行统计",
      "DONE",
      `${job.result.iterations || 0} 次模型迭代 · ${
        job.result.compactions || 0
      } 次上下文压缩${budgetText}`,
    ),
  );
}

async function connectWorkspace(path) {
  const cleaned = path.trim();
  if (!cleaned) return;
  setConnected(false);
  try {
    const overview = await api(`/api/workspace?path=${encodeURIComponent(cleaned)}`);
    state.workspace = overview.path;
    state.sessions = overview.sessions;
    state.knowledge = overview.knowledge;
    state.currentJob = null;
    localStorage.setItem("simple-agent-workspace", state.workspace);
    elements.workspacePath.value = state.workspace;
    const previous = localStorage.getItem(`simple-agent-session:${state.workspace}`);
    const selected = state.sessions.some((item) => item.session_id === previous)
      ? previous
      : state.sessions[0].session_id;
    state.currentSession = selected;
    renderSessions();
    renderKnowledge();
    renderWorkflow(null);
    await selectSession(selected);
    elements.memoryStatus.textContent = `${state.sessions.length} 会话 · ${state.knowledge.length} 资料`;
    setConnected(true);
  } catch (error) {
    showToast(error.message, true);
    setConnected(false);
  }
}

async function selectSession(sessionId) {
  if (state.polling && sessionId !== state.currentSession) {
    showToast("当前需求执行完成后才能切换会话。", true);
    return;
  }
  state.currentSession = sessionId;
  localStorage.setItem(`simple-agent-session:${state.workspace}`, sessionId);
  renderSessions();
  try {
    const summaries = await api(
      `/api/sessions/${encodeURIComponent(sessionId)}/requirements?${queryWorkspace()}`,
    );
    state.requirements = await Promise.all(
      summaries.map(async (summary) => {
        try {
          const detail = await api(
            `/api/requirements/${encodeURIComponent(summary.task_id)}?${queryWorkspace()}`,
          );
          return { ...summary, ...detail };
        } catch {
          return summary;
        }
      }),
    );
    renderTranscript();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function createSession(event) {
  event.preventDefault();
  const title = elements.sessionName.value.trim();
  if (!title || !state.workspace) return;
  try {
    const session = await api("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace: state.workspace, title }),
    });
    state.sessions.push(session);
    elements.sessionDialog.close();
    elements.sessionName.value = "";
    await selectSession(session.session_id);
    elements.memoryStatus.textContent = `${state.sessions.length} 会话 · ${state.knowledge.length} 资料`;
    showToast("会话已创建。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function uploadKnowledge() {
  const files = [...elements.knowledgeFiles.files];
  if (!files.length || !state.workspace) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  elements.uploadButton.disabled = true;
  try {
    await api(`/api/knowledge/upload?${queryWorkspace()}`, {
      method: "POST",
      body: form,
    });
    state.knowledge = await api(`/api/knowledge?${queryWorkspace()}`);
    renderKnowledge();
    elements.memoryStatus.textContent = `${state.sessions.length} 会话 · ${state.knowledge.length} 资料`;
    showToast(`已导入 ${files.length} 个知识文件。`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    elements.knowledgeFiles.value = "";
    elements.uploadButton.disabled = false;
  }
}

async function removeKnowledge(document) {
  if (!window.confirm(`从知识库删除“${document.source_name}”？`)) return;
  try {
    await api(
      `/api/knowledge/${encodeURIComponent(document.document_id)}?${queryWorkspace()}`,
      { method: "DELETE" },
    );
    state.knowledge = state.knowledge.filter(
      (item) => item.document_id !== document.document_id,
    );
    renderKnowledge();
    elements.memoryStatus.textContent = `${state.sessions.length} 会话 · ${state.knowledge.length} 资料`;
    showToast("知识文件已删除。");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function submitRequirement(event) {
  event.preventDefault();
  const request = elements.requestInput.value.trim();
  if (!request || !state.currentSession || state.polling) return;
  const mode = document.querySelector('input[name="agent-mode"]:checked').value;
  state.polling = true;
  elements.sendButton.disabled = true;
  try {
    const job = await api("/api/requirements", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace: state.workspace,
        session_id: state.currentSession,
        request,
        agent_mode: mode,
      }),
    });
    state.currentJob = job;
    elements.requestInput.value = "";
    renderTranscript();
    renderWorkflow(job);
    await pollJob(job.job_id);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.polling = false;
    elements.sendButton.disabled = false;
  }
}

async function pollJob(jobId) {
  while (state.polling) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    state.currentJob = job;
    renderTranscript();
    renderWorkflow(job);
    if (job.status === "completed" || job.status === "failed") {
      if (job.status === "completed") {
        state.sessions = await api(`/api/sessions?${queryWorkspace()}`);
        renderSessions();
        await selectSession(state.currentSession);
        showToast("需求执行完成。");
      } else {
        renderTranscript();
        showToast(job.error || "需求执行失败。", true);
      }
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 850));
  }
}

elements.workspaceForm.addEventListener("submit", (event) => {
  event.preventDefault();
  connectWorkspace(elements.workspacePath.value);
});
elements.newSessionButton.addEventListener("click", () => {
  elements.sessionDialog.showModal();
  window.setTimeout(() => elements.sessionName.focus(), 0);
});
elements.cancelSession.addEventListener("click", () => elements.sessionDialog.close());
elements.sessionForm.addEventListener("submit", createSession);
elements.uploadButton.addEventListener("click", () => elements.knowledgeFiles.click());
elements.knowledgeFiles.addEventListener("change", uploadKnowledge);
elements.composer.addEventListener("submit", submitRequirement);
elements.requestInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

async function bootstrap() {
  setConnected(false);
  try {
    const settings = await api("/api/bootstrap");
    const saved = localStorage.getItem("simple-agent-workspace");
    elements.workspacePath.value = saved || settings.default_workspace;
    await connectWorkspace(elements.workspacePath.value);
  } catch (error) {
    showToast(error.message, true);
  }
}

bootstrap();
