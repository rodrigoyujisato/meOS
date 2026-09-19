/**
 * rysOS Web Client & PWA Controller
 */

const API_BASE = "/api";

// State
let dashboardState = null;

// DOM Elements
const syncBtn = document.getElementById("sync-btn");
const syncStatusText = document.getElementById("sync-status-text");
const authBadge = document.getElementById("auth-badge");
const geminiBadge = document.getElementById("gemini-badge");
const cockpitContentEl = document.getElementById("cockpit-content");
const projectsListEl = document.getElementById("projects-list");
const decisionsListEl = document.getElementById("decisions-list");
const chatMessagesEl = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");

// Modals
const quickCaptureModal = document.getElementById("quick-capture-modal");
const newDecisionModal = document.getElementById("new-decision-modal");

// Initialize Application
document.addEventListener("DOMContentLoaded", async () => {
  // Register Service Worker for PWA
  if ("serviceWorker" in navigator) {
    try {
      await navigator.serviceWorker.register("/sw.js");
      console.log("[PWA] Service Worker registrado com sucesso.");
    } catch (e) {
      console.log("[PWA] Erro ao registrar SW:", e);
    }
  }

  // Load Initial Data
  await loadStatus();
  await loadDashboard();
});

// Load System Status
async function loadStatus() {
  try {
    const res = await fetch(`${API_BASE}/status`);
    const data = await res.json();

    // Google Auth
    if (data.google_authenticated) {
      authBadge.className = "badge badge-success";
      authBadge.innerHTML = '<span class="badge-dot"></span> Google Conectado';
    } else {
      authBadge.className = "badge badge-warning";
      authBadge.innerHTML = '<span class="badge-dot"></span> Google Pendente';
      authBadge.style.cursor = "pointer";
      authBadge.onclick = triggerGoogleAuth;
    }

    // Gemini
    if (data.gemini_ready) {
      geminiBadge.className = "badge badge-success";
      geminiBadge.innerHTML = '<span class="badge-dot"></span> Gemini Ativo';
    } else {
      geminiBadge.className = "badge badge-warning";
      geminiBadge.innerHTML = '<span class="badge-dot"></span> Gemini Sem Chave';
    }

    // Token Observability Badge
    const tokensToday = data.tokens_today || {};
    const count = tokensToday.total_tokens || 0;
    const cost = tokensToday.cost_usd || 0.0;
    const tokensTextEl = document.getElementById("tokens-text");
    if (tokensTextEl) {
      tokensTextEl.innerText = `${count.toLocaleString()} tokens ($${cost.toFixed(4)})`;
    }
  } catch (e) {
    console.error("Erro ao carregar status:", e);
  }
}

// Load Dashboard Data
async function loadDashboard() {
  try {
    const res = await fetch(`${API_BASE}/dashboard`);
    dashboardState = await res.json();
    renderDashboard();
  } catch (e) {
    console.error("Erro ao carregar dashboard:", e);
    cockpitContentEl.innerHTML = "<p style='color: #ef4444;'>Erro ao carregar dados do cofre.</p>";
  }
}

// Render Dashboard View
function renderDashboard() {
  if (!dashboardState) return;

  // Render Cockpit
  if (dashboardState.cockpit_content) {
    cockpitContentEl.innerHTML = parseMarkdownBasic(dashboardState.cockpit_content);
  } else {
    cockpitContentEl.innerHTML = `
      <div style="text-align: center; padding: 2rem 0; color: #94a3b8;">
        <p>Nenhum Cockpit gerado para hoje (${dashboardState.today}).</p>
        <button class="btn btn-primary" style="margin-top: 1rem;" onclick="triggerSync()">
          🚀 Gerar Cockpit Diário Agora
        </button>
      </div>
    `;
  }

  // Render Projects
  if (dashboardState.projects && dashboardState.projects.length > 0) {
    projectsListEl.innerHTML = dashboardState.projects
      .map(
        (p) => `
        <div class="list-item">
          <div>
            <strong>${escapeHtml(p.title)}</strong>
            <div style="font-size: 0.75rem; color: #94a3b8;">Status: ${p.status}</div>
          </div>
          <span class="priority-tag priority-${p.priority.toLowerCase()}">${p.priority}</span>
        </div>
      `
      )
      .join("");
  } else {
    projectsListEl.innerHTML = "<p style='color: #94a3b8; font-size: 0.85rem;'>Nenhum projeto registrado.</p>";
  }

  // Render Decisions
  if (dashboardState.decisions && dashboardState.decisions.length > 0) {
    decisionsListEl.innerHTML = dashboardState.decisions
      .map(
        (d) => `
        <div class="list-item">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem;">${escapeHtml(d.title)}</div>
            <div style="font-size: 0.7rem; color: #94a3b8;">${d.id} • ${d.date}</div>
          </div>
          <span class="priority-tag priority-${d.priority.toLowerCase()}">${d.priority}</span>
        </div>
      `
      )
      .join("");
  } else {
    decisionsListEl.innerHTML = "<p style='color: #94a3b8; font-size: 0.85rem;'>Nenhuma decisão recente.</p>";
  }
}

// Trigger Manual Sync
async function triggerSync() {
  syncBtn.disabled = true;
  syncBtn.innerHTML = "⏳ Sincronizando...";
  try {
    const res = await fetch(`${API_BASE}/sync`, { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      await loadDashboard();
      alert(`Sincronização concluída! Cockpit gerado: ${data.target_date}`);
    } else {
      alert(`Erro na sincronização: ${data.detail || "Falha desconhecida"}`);
    }
  } catch (e) {
    alert(`Erro de conexão: ${e.message}`);
  } finally {
    syncBtn.disabled = false;
    syncBtn.innerHTML = "🔄 Sincronizar Agora";
  }
}

// Trigger Google Auth
async function triggerGoogleAuth() {
  if (confirm("Deseja iniciar a autorização com o Google Workspace no navegador?")) {
    try {
      const res = await fetch(`${API_BASE}/auth/google/start`, { method: "POST" });
      const data = await res.json();
      alert(data.message || "Processo iniciado. Verifique o navegador.");
    } catch (e) {
      alert("Erro ao iniciar autenticação: " + e.message);
    }
  }
}

// Quick Capture Placeholder Helper
function updateQcPlaceholder() {
  const cat = document.getElementById("qc-category").value;
  const textarea = document.getElementById("qc-text");
  const placeholders = {
    today: "Descreva a ação ou foco de hoje (ex: Revisar deck de M&A antes das 15h)...",
    inbox: "Descreva a pendência ou instrução para a IA analisar no próximo ciclo...",
    idea: "Escreva aqui seu insight, anotação ou ideia criativa...",
    framework: "Nome do framework/artigo e resumo conceitual ou link...",
    project: "Título do projeto na 1ª linha e objetivo/outcome nas linhas seguintes...",
    decision: "Título da decisão na 1ª linha e contexto/critérios nas linhas seguintes...",
    person: "Nome da pessoa na 1ª linha e cargo/empresa/e-mail nas linhas seguintes...",
  };
  textarea.placeholder = placeholders[cat] || "Escreva aqui seu conteúdo...";
}

// Step Navigation for Quick Capture
function backToQcInput() {
  const stepReview = document.getElementById("qc-step-review");
  const stepInput = document.getElementById("qc-step-input");
  if (stepReview && stepInput) {
    stepReview.style.display = "none";
    stepInput.style.display = "block";
  }
}

// AI Refinement with Preview & Approval
async function refineQuickCapture() {
  const text = document.getElementById("qc-text").value.trim();
  const category = document.getElementById("qc-category").value;
  if (!text) {
    alert("Por favor, digite o conteúdo antes de refinar com a IA.");
    return;
  }

  const btn = document.getElementById("btn-refine-ia");
  const origBtnText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = "⏳ Refinando com Gemini...";

  try {
    const res = await fetch(`${API_BASE}/quick-capture/refine`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, category }),
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    const data = await res.json();
    document.getElementById("qc-refined-title").value = data.title || "";
    document.getElementById("qc-refined-content").value = data.content || "";
    document.getElementById("qc-refined-summary").textContent = data.summary ? `💡 ${data.summary}` : "";

    // Switch to Review Step
    document.getElementById("qc-step-input").style.display = "none";
    document.getElementById("qc-step-review").style.display = "block";
  } catch (e) {
    alert("Erro ao refinar com IA: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origBtnText;
  }
}

// Direct Quick Capture Submission (sem IA)
async function submitQuickCapture() {
  const text = document.getElementById("qc-text").value.trim();
  const select = document.getElementById("qc-category");
  const category = select.value;
  const destinationName = select.options[select.selectedIndex].text;
  if (!text) return;

  try {
    const res = await fetch(`${API_BASE}/quick-capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, category }),
    });
    if (res.ok) {
      document.getElementById("qc-text").value = "";
      closeModal("quick-capture-modal");
      await loadDashboard();
      alert(`Salvo com sucesso em: ${destinationName}!`);
    } else {
      const err = await res.json();
      alert("Erro ao salvar: " + (err.detail || res.statusText));
    }
  } catch (e) {
    alert("Erro ao capturar: " + e.message);
  }
}

// Approval & Save (com IA)
async function approveAndSaveQuickCapture() {
  const custom_title = document.getElementById("qc-refined-title").value.trim();
  const custom_content = document.getElementById("qc-refined-content").value.trim();
  const select = document.getElementById("qc-category");
  const category = select.value;
  const destinationName = select.options[select.selectedIndex].text;
  const text = document.getElementById("qc-text").value.trim();

  if (!custom_title || !custom_content) {
    alert("Título e conteúdo são obrigatórios.");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/quick-capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        category,
        custom_title,
        custom_content,
      }),
    });

    if (res.ok) {
      document.getElementById("qc-text").value = "";
      document.getElementById("qc-refined-title").value = "";
      document.getElementById("qc-refined-content").value = "";
      backToQcInput();
      closeModal("quick-capture-modal");
      await loadDashboard();
      alert(`Nota aprovada e gravada com sucesso em: ${destinationName}!`);
    } else {
      const err = await res.json();
      alert("Erro ao salvar nota aprovada: " + (err.detail || res.statusText));
    }
  } catch (e) {
    alert("Erro ao salvar: " + e.message);
  }
}

// Decision Submission
async function submitNewDecision() {
  const title = document.getElementById("dec-title").value.trim();
  const context = document.getElementById("dec-context").value.trim();
  const priority = document.getElementById("dec-priority").value;
  if (!title) return;

  try {
    const res = await fetch(`${API_BASE}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, context, priority }),
    });
    if (res.ok) {
      document.getElementById("dec-title").value = "";
      document.getElementById("dec-context").value = "";
      closeModal("new-decision-modal");
      await loadDashboard();
      alert("Decisão registrada com sucesso no Obsidian!");
    }
  } catch (e) {
    alert("Erro ao registrar decisão: " + e.message);
  }
}

// Chat with Agent
async function sendChatMessage() {
  const msg = chatInput.value.trim();
  if (!msg) return;

  // Append user bubble
  appendChatBubble(msg, "user");
  chatInput.value = "";

  // Append temporary loading
  const loadingId = appendChatBubble("Pensando com Gemini...", "agent");

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    document.getElementById(loadingId).innerText = data.response || "Sem resposta.";
  } catch (e) {
    document.getElementById(loadingId).innerText = "Erro ao se comunicar com o agente: " + e.message;
  }
}

function appendChatBubble(text, sender) {
  const bubble = document.createElement("div");
  const bubbleId = "msg-" + Date.now() + Math.random();
  bubble.id = bubbleId;
  bubble.className = `chat-bubble chat-${sender}`;
  bubble.innerText = text;
  chatMessagesEl.appendChild(bubble);
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  return bubbleId;
}

// Modal Helpers
function openModal(id) {
  document.getElementById(id).classList.add("active");
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) {
    el.classList.remove("active");
  }
  if (id === "quick-capture-modal") {
    backToQcInput();
  }
}

// Basic Markdown parser for quick client-side rendering
function parseMarkdownBasic(md) {
  if (!md) return "";
  let html = escapeHtml(md);

  // Headers
  html = html.replace(/^### (.*$)/gim, "<h3>$1</h3>");
  html = html.replace(/^## (.*$)/gim, "<h2>$1</h2>");
  html = html.replace(/^# (.*$)/gim, "<h1>$1</h1>");

  // Bold & Italic
  html = html.replace(/\*\*(.*?)\*\*/gim, "<strong>$1</strong>");
  html = html.replace(/\*(.*?)\*/gim, "<em>$1</em>");

  // Links
  html = html.replace(/\[\[(.*?)\]\]/gim, "<span style='color: #06b6d4; font-weight: 500;'>[[$1]]</span>");
  html = html.replace(/\[(.*?)\]\((.*?)\)/gim, "<a href='$2' target='_blank' style='color: #3b82f6;'>$1</a>");

  // Checkboxes
  html = html.replace(/- \[ \] (.*$)/gim, "<div style='margin: 0.25rem 0;'>⬜ $1</div>");
  html = html.replace(/- \[x\] (.*$)/gim, "<div style='margin: 0.25rem 0;'>✅ $1</div>");

  // Lists
  html = html.replace(/^- (.*$)/gim, "<li>$1</li>");

  // Linebreaks
  html = html.replace(/\n/gim, "<br>");

  return html;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Token Observability Modal
async function openTokenObservabilityModal() {
  openModal("token-observability-modal");
  await loadTokenMetrics();
}

async function loadTokenMetrics() {
  try {
    const res = await fetch(`${API_BASE}/metrics/tokens`);
    const data = await res.json();

    document.getElementById("modal-gemini-model").innerText = data.current_model || "gemini-3.5-flash-lite";

    // 1. Today Cards
    const today = data.today || {};
    document.getElementById("metric-today-tokens").innerText = (today.total_tokens || 0).toLocaleString();
    document.getElementById("metric-today-sub").innerText = `P: ${(today.prompt_tokens || 0).toLocaleString()} | O: ${(today.candidate_tokens || 0).toLocaleString()}`;
    document.getElementById("metric-today-cost").innerText = `$${(today.cost_usd || 0).toFixed(4)}`;
    document.getElementById("metric-today-cost-brl").innerText = `~R$ ${(today.cost_brl || 0).toFixed(4)}`;
    document.getElementById("metric-today-calls").innerText = today.calls_count || 0;

    // Total Card
    const total = data.total || {};
    document.getElementById("metric-total-tokens").innerText = (total.total_tokens || 0).toLocaleString();
    document.getElementById("metric-total-cost").innerText = `$${(total.cost_usd || 0).toFixed(4)} total`;

    // Update Header Badge too
    const tokensTextEl = document.getElementById("tokens-text");
    if (tokensTextEl) {
      tokensTextEl.innerText = `${(today.total_tokens || 0).toLocaleString()} tokens ($${(today.cost_usd || 0).toFixed(4)})`;
    }

    // 2. Operations Table
    const tbody = document.getElementById("token-operations-tbody");
    if (data.by_operation && data.by_operation.length > 0) {
      tbody.innerHTML = data.by_operation.map(op => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
          <td style="padding: 0.5rem 0.75rem; font-weight: 500; color: #f8fafc;">${escapeHtml(op.operation)}</td>
          <td style="padding: 0.5rem 0.75rem; text-align: right; color: #94a3b8;">${op.calls_count}</td>
          <td style="padding: 0.5rem 0.75rem; text-align: right; color: #06b6d4; font-weight: 600;">${op.total_tokens.toLocaleString()}</td>
          <td style="padding: 0.5rem 0.75rem; text-align: right; color: #10b981;">$${op.cost_usd.toFixed(6)}</td>
        </tr>
      `).join("");
    } else {
      tbody.innerHTML = `<tr><td colspan="4" style="padding: 1rem; text-align: center; color: #94a3b8;">Nenhum consumo registrado ainda. Execute uma sincronização ou converse com o agente.</td></tr>`;
    }

    // 3. Recent Logs
    const logsEl = document.getElementById("token-recent-logs");
    if (data.recent_logs && data.recent_logs.length > 0) {
      logsEl.innerHTML = data.recent_logs.map(log => `
        <div style="padding: 0.25rem 0; border-bottom: 1px dashed rgba(255,255,255,0.05); display: flex; justify-content: space-between; gap: 0.5rem;">
          <span><span style="color: #64748b;">${escapeHtml(log.created_at.slice(11, 19))}</span> <strong>${escapeHtml(log.operation)}</strong></span>
          <span><span style="color: #06b6d4;">${log.total_tokens.toLocaleString()} tok</span> <span style="color: #10b981;">($${log.cost_usd.toFixed(5)})</span></span>
        </div>
      `).join("");
    } else {
      logsEl.innerHTML = `<p style="color: #94a3b8;">Nenhum log recente.</p>`;
    }

  } catch (e) {
    console.error("Erro ao carregar métricas de tokens:", e);
  }
}
