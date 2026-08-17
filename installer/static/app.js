const state = { runId: null, timer: null, requestToken: "", canExecute: false };
const $ = (selector) => document.querySelector(selector);

function setHostStatus(status) {
  state.requestToken = status.request_token || "";
  state.canExecute = status.platform === "Linux" && status.root && status.allow_modify;
  $("#host-status").textContent = `${status.platform} ${status.architecture} · ${state.canExecute ? "真实安装已启用" : "需要 sudo --execute"}`;
  const defaults = status.config_defaults || {};
  const form = $("#install-form");
  ["server_name", "data_root", "backend_jar", "frontend_path"].forEach((key) => {
    if (defaults[key] && form.elements[key]) form.elements[key].value = defaults[key];
  });
  updateStartButton();
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    setHostStatus(await response.json());
  } catch (error) {
    $("#host-status").textContent = "无法连接本地安装器";
  }
}

function updateStartButton() {
  const button = $("#start-button");
  const confirmed = $("#confirm-install").checked;
  button.disabled = !state.canExecute || !confirmed;
  button.title = state.canExecute ? (confirmed ? "" : "请先确认测试环境") : "请使用 sudo 和 --execute 启动安装器";
}

function formData() {
  return Object.fromEntries(new FormData($("#install-form")).entries());
}

function randomHex(bytes) {
  const values = new Uint8Array(bytes);
  crypto.getRandomValues(values);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

function generateSecrets() {
  const form = $("#install-form");
  form.elements.db_password.value = randomHex(16);
  form.elements.rabbit_password.value = randomHex(16);
  form.elements.rabbit_admin_password.value = randomHex(16);
  form.elements.rustfs_access_key.value = randomHex(12);
  form.elements.rustfs_secret.value = randomHex(24);
}

function statusText(task) {
  return { pending: "等待中", running: `${task.progress}%`, completed: "已完成", failed: "失败", blocked: "已阻塞" }[task.status] || task.status;
}

function renderTasks(run) {
  $("#task-list").innerHTML = Object.values(run.tasks).map((task) => {
    const icon = task.status === "completed" ? "✓" : task.status === "failed" ? "!" : task.status === "running" ? "•" : "";
    return `<div class="task-row ${task.status}">
      <span class="task-icon">${icon}</span>
      <div><div class="task-name">${escapeHtml(task.title)}</div><div class="task-detail">${escapeHtml(task.message || task.detail)}</div></div>
      <span class="task-status">${statusText(task)}</span>
    </div>`;
  }).join("");
}

function renderLogs(run) {
  if (!run.logs.length) return;
  const consoleElement = $("#log-console");
  consoleElement.innerHTML = run.logs.map((entry) => `<div class="log-entry ${entry.level}">
    <span class="log-time">${entry.time.slice(11, 19)}</span><span class="log-message">[${escapeHtml(entry.task)}] ${escapeHtml(entry.message)}</span>
  </div>`).join("");
  consoleElement.scrollTop = consoleElement.scrollHeight;
  $("#log-count").textContent = `${run.logs.length} 条`;
  $("#run-state").textContent = run.status === "completed" ? "安装完成" : run.status === "failed" ? "安装失败" : "正在安装";
}

function renderRun(run) {
  $("#progress-panel").classList.remove("hidden");
  $("#overall-number").textContent = `${run.progress}%`;
  $("#overall-fill").style.width = `${run.progress}%`;
  $("#run-title").textContent = run.status === "completed" ? "安装和验收完成" : run.status === "failed" ? "安装未完成" : "正在执行安装计划";
  renderTasks(run);
  renderLogs(run);
  updateSteps(run);
  if (["completed", "failed"].includes(run.status)) {
    clearInterval(state.timer);
    $("#confirm-install").checked = false;
    updateStartButton();
    $("#start-button").innerHTML = `<span>重新执行</span><span class="button-arrow">→</span>`;
  }
}

function updateSteps(run) {
  const completedByStep = [
    run.tasks.preflight?.status === "completed",
    run.tasks.packages?.status === "completed",
    ["mariadb", "redis", "rabbitmq", "jdk", "nginx", "rustfs"].every((id) => run.tasks[id]?.status === "completed"),
    run.tasks.backend?.status === "completed",
    run.tasks.acceptance?.status === "completed",
  ];
  const activeIndex = Math.min(completedByStep.findIndex((value) => !value), 4);
  document.querySelectorAll(".step").forEach((item, index) => {
    item.classList.toggle("active", index === (activeIndex < 0 ? 4 : activeIndex));
    item.classList.toggle("done", completedByStep[index]);
  });
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const response = await fetch(`/api/runs/${state.runId}`);
    if (!response.ok) throw new Error("安装记录不存在");
    renderRun(await response.json());
  } catch (error) {
    clearInterval(state.timer);
    $("#run-state").textContent = "无法读取运行状态";
  }
}

async function startRun(event) {
  event.preventDefault();
  if (!state.canExecute || !$("#confirm-install").checked) return;
  const button = $("#start-button");
  button.disabled = true;
  button.innerHTML = `<span>正在启动</span><span class="button-arrow">·</span>`;
  try {
    const response = await fetch("/api/install", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Installer-Token": state.requestToken },
      body: JSON.stringify(formData()),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "无法启动安装计划");
    state.runId = result.id;
    $("#progress-panel").classList.remove("hidden");
    clearInterval(state.timer);
    state.timer = setInterval(pollRun, 700);
    await pollRun();
  } catch (error) {
    button.innerHTML = `<span>开始安装</span><span class="button-arrow">→</span>`;
    $("#run-state").textContent = error.message;
    updateStartButton();
  }
}

function resetForm() {
  $("#install-form").reset();
  $("#progress-panel").classList.add("hidden");
  $("#log-console").innerHTML = `<div class="empty-log"><span class="empty-mark">_</span><p>开始安装后，任务日志会显示在这里。</p></div>`;
  $("#log-count").textContent = "0 条";
  $("#run-state").textContent = "尚未开始";
  $("#start-button").innerHTML = `<span>开始安装</span><span class="button-arrow">→</span>`;
  clearInterval(state.timer);
  state.runId = null;
  updateStartButton();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

$("#install-form").addEventListener("submit", startRun);
$("#reset-button").addEventListener("click", resetForm);
$("#generate-secrets").addEventListener("click", generateSecrets);
$("#confirm-install").addEventListener("change", updateStartButton);
$("#clear-log").addEventListener("click", () => { $("#log-console").innerHTML = ""; $("#log-count").textContent = "0 条"; });
loadStatus();
