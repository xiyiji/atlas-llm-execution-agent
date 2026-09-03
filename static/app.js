const $ = (selector) => document.querySelector(selector);
const state = { task: null, source: null, events: [], activeAgent: "" };

async function ensureSession() {
  const key = $("#apiKey").value.trim();
  if (!key) return;
  const response = await fetch("/api/session", {method: "POST", headers: {"X-API-Key": key}});
  if (!response.ok) throw new Error("Invalid production API key");
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

function renderMarkdown(value) {
  let text = escapeHtml(value || "");
  text = text.replace(/```(?:\w+)?\n([\s\S]*?)```/g, "<pre>$1</pre>");
  text = text.replace(/^### (.+)$/gm, "<h3>$1</h3>").replace(/^## (.+)$/gm, "<h2>$1</h2>");
  text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const chunks = text.split(/\n{2,}/).map(block => /^<(h\d|pre)/.test(block) ? block : `<p>${block.replace(/\n/g, "<br>")}</p>`);
  return chunks.join("");
}

function setConnection(online, label) {
  $(".live").classList.toggle("online", online);
  $("#connectionLabel").textContent = label;
}

function renderBanner(health) {
  const banner = $("#providerBanner");
  if (!health || health.demo || health.checks?.model_provider !== false) { banner.hidden = true; return; }
  banner.innerHTML = `<b>Model provider "${escapeHtml(health.provider)}" is not ready.</b> ${escapeHtml(health.provider_problem)}<br>${escapeHtml(health.provider_hint).replace(/`([^`]+)`/g, "<code>$1</code>")}`;
  banner.hidden = false;
}

async function loadHealth() {
  try {
    const health = await fetch("/api/health").then(r => r.json());
    const ready = health.checks?.model_provider !== false;
    setConnection(ready, health.demo ? "Live · demo" : ready ? `Live · ${health.provider}` : `${health.provider} not ready`);
    renderBanner(health);
    const needsKey = Boolean(health.auth_required);
    $(".api-key-label").hidden = !needsKey;
    $("#apiKey").hidden = !needsKey;
  } catch (_) { setConnection(false, "Offline"); }
}

async function loadMemory() {
  try {
    await ensureSession();
    const response = await fetch("/api/memory?limit=8");
    if (!response.ok) throw new Error("Memory unavailable");
    const items = await response.json();
    $("#memoryList").innerHTML = items.length ? items.slice().reverse().map(item => {
      const date = new Date(item.ts * 1000);
      return `<article class="memory-item"><span class="meta">${escapeHtml(item.outcome)}</span><time>${date.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}</time><p>${escapeHtml(item.goal)}</p></article>`;
    }).join("") : '<div class="empty">No past runs yet.</div>';
  } catch (_) { $("#memoryList").innerHTML = '<div class="empty">Memory unavailable.</div>'; }
}

function renderTask() {
  const task = state.task;
  if (!task) return;
  $("#activeGoal").textContent = task.goal;
  const pill = $("#statusPill");
  pill.textContent = task.status.replaceAll("_", " ");
  pill.className = `status-pill ${task.status}`;
  $("#approvalBox").hidden = task.status !== "awaiting_approval";

  const risk = task.risk;
  if (risk) {
    $("#riskValue").textContent = `${risk.score}/100 · ${risk.level.toUpperCase()}`;
    const bar = $("#riskBar");
    bar.style.width = `${risk.score}%`;
    bar.style.background = risk.score >= 60 ? "var(--red)" : risk.score >= 30 ? "var(--amber)" : "var(--green)";
    $("#riskFactors").innerHTML = risk.factors.map(f => `<li>${escapeHtml(f)}</li>`).join("");
  }

  $("#taskList").innerHTML = task.steps?.length ? task.steps.map(step => `
    <div class="task-row ${step.status}">
      <span class="step-status">${escapeHtml(step.status === "completed" ? "done" : step.status)}</span>
      <span class="step-agent">${escapeHtml(step.agent)}</span>
      <span class="step-title">${escapeHtml(step.title)}</span>
    </div>`).join("") : '<div class="empty">The Planner is building the task graph…</div>';

  document.querySelectorAll("#committee [data-agent]").forEach(node => {
    const name = node.dataset.agent;
    const agentSteps = task.steps?.filter(step => step.agent === name) || [];
    node.classList.toggle("done", name === "safety" ? Boolean(risk) : agentSteps.length > 0 && agentSteps.every(step => step.status === "completed"));
    node.classList.toggle("active", state.activeAgent === name);
  });

  if (task.status === "failed" || task.status === "denied") {
    const providerError = state.events.find(e => e.type === "provider.error");
    const hint = providerError ? `<p>${escapeHtml(providerError.data?.hint || "")}</p>` : "";
    $("#report").innerHTML = `<div class="failure"><h3>${task.status === "denied" ? "Stopped at the approval gate" : "Task failed"}</h3><p>${escapeHtml(task.error || task.result || "")}</p>${hint}</div>`;
  } else if (task.result) $("#report").innerHTML = renderMarkdown(task.result);
}

function addEvent(event) {
  if (!event || !event.type) return;
  state.events.push(event);
  state.events = state.events.slice(-150);
  const activity = $("#activity");
  activity.innerHTML = state.events.map(item => {
    const time = new Date(item.ts * 1000).toLocaleTimeString([], {hour12: false});
    const agent = item.agent || "system";
    return `<div class="event-row"><time>${time}</time><b class="${escapeHtml(agent)}">${escapeHtml(agent)}</b><span>${escapeHtml(item.message)}</span></div>`;
  }).join("");
  activity.scrollTop = activity.scrollHeight;
}

async function refreshTask() {
  if (!state.task) return;
  const response = await fetch(`/api/tasks/${state.task.id}`);
  if (response.ok) { state.task = await response.json(); renderTask(); }
}

function applyEvent(event) {
  addEvent(event);
  if (event.agent && ["agent.started", "step.started"].includes(event.type)) state.activeAgent = event.agent;
  if (["step.completed", "step.failed", "provider.error", "risk.assessed", "plan.created", "task.status", "task.completed", "task.failed", "approval.required"].includes(event.type)) refreshTask();
  if (["step.completed", "step.failed", "task.completed", "task.failed", "stream.end"].includes(event.type)) state.activeAgent = "";
  if (event.type === "stream.end") {
    state.source?.close();
    state.source = null;
    $("#runButton").disabled = false;
    refreshTask().then(loadMemory);
  }
  renderTask();
}

function connect(taskId) {
  state.source?.close();
  const source = new EventSource(`/api/tasks/${taskId}/events`);
  state.source = source;
  source.addEventListener("snapshot", message => {
    state.task = JSON.parse(message.data).task;
    renderTask();
  });
  const types = ["task.created", "task.queued", "task.recovered", "task.status", "agent.started", "plan.created", "risk.assessed", "approval.required", "approval.auto", "approval.resolved", "approval.timeout", "step.started", "step.retry", "step.completed", "step.failed", "provider.error", "rework.started", "task.completed", "task.failed", "stream.end"];
  types.forEach(type => source.addEventListener(type, message => applyEvent(JSON.parse(message.data))));
  source.onerror = () => { if (state.source) setConnection(false, "Reconnecting"); };
  source.onopen = () => loadHealth();
}

async function runTask() {
  const goal = $("#goal").value.trim();
  if (!goal) { $("#formError").textContent = "Enter a goal first."; return; }
  $("#formError").textContent = "";
  $("#runButton").disabled = true;
  state.events = [];
  $("#activity").innerHTML = '<div class="empty">Connecting to the event stream…</div>';
  $("#report").innerHTML = '<div class="empty">Committee execution in progress…</div>';
  try {
    await ensureSession();
    const response = await fetch("/api/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({goal, auto_approve: $("#autoApprove").checked})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Task could not be created");
    state.task = data;
    renderTask();
    connect(data.id);
  } catch (error) {
    $("#formError").textContent = error.message;
    $("#runButton").disabled = false;
  }
}

async function decide(approved) {
  if (!state.task) return;
  $("#approveButton").disabled = $("#denyButton").disabled = true;
  try {
    const response = await fetch(`/api/tasks/${state.task.id}/approval`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({approved})});
    if (!response.ok) throw new Error((await response.json()).detail);
  } catch (error) { $("#formError").textContent = error.message; }
  finally { $("#approveButton").disabled = $("#denyButton").disabled = false; }
}

$("#runButton").addEventListener("click", runTask);
$("#approveButton").addEventListener("click", () => decide(true));
$("#denyButton").addEventListener("click", () => decide(false));
document.querySelectorAll("[data-goal]").forEach(button => button.addEventListener("click", () => $("#goal").value = button.dataset.goal));
loadHealth();
loadMemory();
