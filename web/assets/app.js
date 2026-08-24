/**
 * GDAP web UI.
 *
 * A deliberate design decision (ADR-007): no build step, no framework. The UI is a *client of the
 * public API* — the same endpoints the CLI and any third-party integration use — so it must never
 * become the place where behaviour lives. If a screen needs something the API cannot answer, the
 * fix belongs in the API.
 */

const state = { page: "dashboard", apiKey: localStorage.getItem("gdap.apiKey") || "", info: null };

// ────────────────────────────────────────── api client ────────────────────────────────────

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.apiKey) headers["X-API-Key"] = state.apiKey;
  const response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { raw: text }; }
  if (!response.ok) {
    const error = payload?.error || { message: response.statusText, code: response.status };
    throw Object.assign(new Error(error.message), { code: error.code, details: error.details, trace: error.trace_id });
  }
  return payload;
}

// ────────────────────────────────────────── rendering ─────────────────────────────────────

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
};

const badge = (value) => el("span", { class: `badge ${value ?? "info"}` }, value ?? "—");
const num = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString());
const short = (value, n = 8) => (value ? String(value).slice(0, n) : "—");
const when = (value) => (value ? String(value).replace("T", " ").slice(0, 19) : "—");

function table(columns, rows, { onRow } = {}) {
  if (!rows.length) return el("div", { class: "empty" }, "nothing here yet");
  const head = el("tr", {}, columns.map((column) => el("th", {}, column.label)));
  const body = rows.map((row) => {
    const tr = el("tr", onRow ? { class: "clickable", onclick: () => onRow(row) } : {},
      columns.map((column) => {
        const value = column.render ? column.render(row) : row[column.key];
        return el("td", { class: column.numeric ? "num" : "" }, value === null || value === undefined ? "—" : value);
      }));
    return tr;
  });
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}

function card(title, ...children) { return el("div", { class: "card" }, el("h3", {}, title), ...children); }
function stat(title, value, sub) {
  return card(title, el("div", { class: "stat" }, value, sub ? el("small", {}, ` ${sub}`) : null));
}

function toast(message, isError = false) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = `toast${isError ? " error" : ""}`;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 5200);
}

function insight(item) {
  const evidence = (item.evidence || [])
    .map((e) => [e.source, e.calculation || e.query, e.rows_considered ? `${e.rows_considered} rows` : null]
      .filter(Boolean).join(" · "))
    .join("  |  ");
  return el("div", { class: `insight ${item.kind}` },
    el("div", { class: "t" }, `${item.kind === "fact" ? "" : `[${item.kind}] `}${item.title}`),
    item.detail ? el("div", { class: "d" }, item.detail) : null,
    el("div", { class: "e" }, `confidence ${Math.round((item.confidence ?? 0) * 100)}%${evidence ? ` · ${evidence}` : ""}`));
}

// ─────────────────────────────────────────── pages ────────────────────────────────────────

const pages = {
  dashboard: {
    title: "Dashboard", subtitle: "What the platform knows right now",
    async render() {
      const data = await api("/api/v1/system/dashboard");
      const quality = data.quality.average;
      return el("div", {},
        el("div", { class: "grid" },
          stat("Datasets", num(data.counts.datasets)),
          stat("Rows under management", num(data.counts.rows_total)),
          stat("Sources", num(data.counts.sources)),
          stat("Pipelines", num(data.counts.pipelines)),
          stat("Average quality", quality === null ? "—" : quality.toFixed(1), "/100"),
          stat("Open alerts", num(data.counts.alerts_open))),
        el("div", { class: "section-title" }, el("h2", {}, "Recent runs")),
        table([
          { label: "job", render: (r) => el("code", {}, short(r.id)) },
          { label: "pipeline", key: "pipeline" },
          { label: "state", render: (r) => badge(r.state) },
          { label: "duration", numeric: true, render: (r) => (r.duration_seconds ? `${r.duration_seconds.toFixed(2)}s` : "—") },
          { label: "created", render: (r) => when(r.created_at) },
        ], data.recent_jobs, { onRow: (r) => go("jobs", { job: r.id }) }),
        data.open_alerts.length ? el("div", {},
          el("div", { class: "section-title" }, el("h2", {}, "Open alerts")),
          table([
            { label: "severity", render: (r) => badge(r.severity) },
            { label: "alert", key: "title" },
          ], data.open_alerts)) : null,
        el("div", { class: "section-title" }, el("h2", {}, "Catalog")),
        table([
          { label: "dataset", key: "dataset" },
          { label: "rows", numeric: true, render: (r) => num(r.rows) },
          { label: "versions", numeric: true, key: "versions" },
          { label: "quality", numeric: true, render: (r) => (r.quality_score ? r.quality_score.toFixed(1) : "—") },
          { label: "classification", render: (r) => badge(r.classification.toLowerCase()) },
        ], data.datasets, { onRow: (r) => go("datasets", { dataset: r.dataset }) }));
    },
  },

  sources: {
    title: "Sources", subtitle: "Where data comes from",
    async render() {
      const [{ items }, connectors] = await Promise.all([api("/api/v1/sources"), api("/api/v1/system/connectors")]);
      const form = el("form", { class: "stack", onsubmit: async (event) => {
        event.preventDefault();
        const data = new FormData(event.target);
        try {
          await api("/api/v1/sources", { method: "POST", body: {
            name: data.get("name"),
            type: String(data.get("connector")).split(".")[0],
            connector: data.get("connector"),
            config: JSON.parse(data.get("config") || "{}"),
          }});
          toast("source registered");
          go("sources");
        } catch (error) { toast(`${error.message}`, true); }
      }},
        el("label", {}, "name"), el("input", { type: "text", name: "name", required: "true", placeholder: "sales_files" }),
        el("label", {}, "connector"),
        el("select", { name: "connector" }, connectors.items.map((c) => el("option", { value: c.key }, `${c.key} — ${c.title}`))),
        el("label", {}, "config (JSON)"),
        el("textarea", { name: "config", placeholder: '{"path": "/data/sales", "pattern": "*.csv"}' }),
        el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit" }, "Register source")));

      return el("div", {},
        table([
          { label: "name", key: "name" },
          { label: "connector", render: (r) => el("code", {}, r.connector) },
          { label: "status", render: (r) => badge(r.status) },
          { label: "classification", render: (r) => badge(r.classification.toLowerCase()) },
          { label: "last tested", render: (r) => when(r.last_tested_at) },
          { label: "", render: (r) => el("button", { class: "btn", onclick: async (event) => {
              event.stopPropagation();
              try { const result = await api(`/api/v1/sources/${r.name}/test`, { method: "POST" });
                toast(result.ok ? `${r.name}: ${result.message}` : `${r.name}: ${result.message}`, !result.ok);
              } catch (error) { toast(error.message, true); }
            }}, "test") },
        ], items),
        el("div", { class: "section-title" }, el("h2", {}, "Register a source")),
        card("New source", form));
    },
  },

  datasets: {
    title: "Datasets", subtitle: "The catalog, with quality and lineage",
    async render(params) {
      if (params.dataset) return renderDataset(params.dataset);
      const { items } = await api("/api/v1/datasets");
      return table([
        { label: "dataset", key: "name" },
        { label: "rows", numeric: true, render: (r) => num(r.row_count) },
        { label: "version", numeric: true, key: "current_version" },
        { label: "quality", numeric: true, render: (r) => (r.quality_score ? r.quality_score.toFixed(1) : "—") },
        { label: "classification", render: (r) => badge(r.classification.toLowerCase()) },
        { label: "updated", render: (r) => when(r.updated_at) },
      ], items, { onRow: (r) => go("datasets", { dataset: r.name }) });
    },
  },

  pipelines: {
    title: "Pipelines", subtitle: "Declarative automation",
    async render() {
      const { items } = await api("/api/v1/pipelines");
      return el("div", {},
        table([
          { label: "name", key: "name" },
          { label: "v", numeric: true, key: "version" },
          { label: "steps", numeric: true, key: "step_count" },
          { label: "enabled", render: (r) => badge(r.enabled ? "ok" : "info") },
          { label: "next run", render: (r) => when(r.next_run_at) },
          { label: "last", render: (r) => (r.last_state ? badge(r.last_state) : "—") },
          { label: "", render: (r) => el("button", { class: "btn primary", onclick: async (event) => {
              event.stopPropagation();
              toast(`running ${r.name}…`);
              try {
                const result = await api(`/api/v1/pipelines/${r.name}/run`, { method: "POST", body: { wait: true } });
                toast(`${r.name}: ${result.state}`, result.state !== "SUCCESS");
                go("jobs", { job: result.job_id });
              } catch (error) { toast(error.message, true); }
            }}, "run") },
        ], items),
        el("div", { class: "section-title" }, el("h2", {}, "Generate from a description")),
        card("Natural language → pipeline",
          el("form", { class: "stack", onsubmit: async (event) => {
            event.preventDefault();
            const request = new FormData(event.target).get("request");
            try {
              const result = await api("/api/v1/agents/plan", { method: "POST", body: { request: String(request), create: true } });
              toast(`planned '${result.plan.spec.name}' — review it before running`);
              go("pipelines");
            } catch (error) { toast(error.message, true); }
          }},
            el("textarea", { name: "request", placeholder: "Clean the transactions, compute revenue per region, compare with last month and generate a report" }),
            el("div", { class: "row" },
              el("button", { class: "btn primary", type: "submit" }, "Plan pipeline"),
              el("span", { class: "muted" }, "the generated pipeline is stored for review, never auto-executed")))));
    },
  },

  jobs: {
    title: "Jobs", subtitle: "Every run, with its steps and outcome",
    async render(params) {
      if (params.job) return renderJob(params.job);
      const { items } = await api("/api/v1/jobs?limit=40");
      return table([
        { label: "job", render: (r) => el("code", {}, short(r.id)) },
        { label: "pipeline", key: "pipeline" },
        { label: "state", render: (r) => badge(r.state) },
        { label: "trigger", key: "trigger" },
        { label: "attempt", render: (r) => `${r.attempt}/${r.max_attempts}` },
        { label: "duration", numeric: true, render: (r) => ((r.metrics || {}).duration_seconds ? `${r.metrics.duration_seconds.toFixed(2)}s` : "—") },
        { label: "created", render: (r) => when(r.created_at) },
      ], items, { onRow: (r) => go("jobs", { job: r.id }) });
    },
  },

  reports: {
    title: "Reports", subtitle: "Generated artifacts",
    async render() {
      const [{ items }, datasets] = await Promise.all([api("/api/v1/reports"), api("/api/v1/datasets")]);
      return el("div", {},
        table([
          { label: "report", key: "name" },
          { label: "format", render: (r) => badge(r.format) },
          { label: "size", numeric: true, render: (r) => `${(r.size_bytes / 1024).toFixed(1)} KiB` },
          { label: "dataset", key: "dataset" },
          { label: "created", render: (r) => when(r.created_at) },
          { label: "", render: (r) => el("a", { href: r.format === "html" ? `/api/v1/reports/${r.id}/view` : r.download_url, target: "_blank", rel: "noopener" }, r.format === "html" ? "view ↗" : "download") },
        ], items),
        el("div", { class: "section-title" }, el("h2", {}, "Generate a report")),
        card("New report",
          el("form", { class: "stack", onsubmit: async (event) => {
            event.preventDefault();
            const data = new FormData(event.target);
            toast("generating…");
            try {
              await api("/api/v1/reports", { method: "POST", body: {
                dataset: data.get("dataset"), formats: [data.get("format")],
              }});
              toast("report generated");
              go("reports");
            } catch (error) { toast(error.message, true); }
          }},
            el("label", {}, "dataset"),
            el("select", { name: "dataset" }, datasets.items.map((d) => el("option", { value: d.name }, d.name))),
            el("label", {}, "format"),
            el("select", { name: "format" }, ["html", "xlsx", "csv", "json", "markdown"].map((f) => el("option", { value: f }, f))),
            el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit" }, "Generate")))));
    },
  },

  ai: {
    title: "AI Analyst", subtitle: "Answers with evidence — never invented numbers",
    async render() {
      const [datasets, agents] = await Promise.all([api("/api/v1/datasets"), api("/api/v1/agents")]);
      const output = el("div", {});
      const form = el("form", { class: "stack", onsubmit: async (event) => {
        event.preventDefault();
        const data = new FormData(event.target);
        output.replaceChildren(el("div", { class: "loading" }, "thinking…"));
        try {
          const answer = await api("/api/v1/agents/ask", { method: "POST", body: {
            question: data.get("question"), dataset: data.get("dataset") || null,
          }});
          output.replaceChildren(
            el("div", { class: "answer" }, answer.answer),
            answer.insights.length ? el("div", { class: "section-title" }, el("h2", {}, "Insights")) : null,
            ...answer.insights.map(insight),
            el("p", { class: "muted mono" },
              `tools: ${answer.tool_calls.map((c) => c.tool).join(", ") || "none"} · provider: ${answer.provider} · confidence: ${Math.round(answer.confidence * 100)}%`),
            ...answer.limitations.map((l) => el("p", { class: "muted" }, `! ${l}`)));
        } catch (error) { output.replaceChildren(el("div", { class: "empty" }, error.message)); }
      }},
        el("label", {}, "question"),
        el("input", { type: "text", name: "question", required: "true", placeholder: "Why did revenue change last month?" }),
        el("label", {}, "dataset"),
        el("select", { name: "dataset" }, [el("option", { value: "" }, "(most recent)"), ...datasets.items.map((d) => el("option", { value: d.name }, d.name))]),
        el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit" }, "Ask")));

      return el("div", {},
        card(`Mode: ${agents.status.mode} (${agents.status.provider})`, form),
        output,
        el("div", { class: "section-title" }, el("h2", {}, "Agents and their tool grants")),
        table([
          { label: "agent", key: "name" },
          { label: "role", key: "description" },
          { label: "tools", render: (r) => el("code", {}, r.tools.join(", ")) },
        ], agents.items));
    },
  },

  governance: {
    title: "Governance", subtitle: "Lineage, audit and classification",
    async render() {
      const [audit, classification, retention] = await Promise.all([
        api("/api/v1/audit?limit=40"), api("/api/v1/classification"), api("/api/v1/retention/candidates"),
      ]);
      return el("div", {},
        el("div", { class: "grid" },
          ...Object.entries(classification).map(([level, names]) => stat(level, names.length, names.slice(0, 3).join(", "))),
          stat("Retention candidates", retention.count)),
        el("div", { class: "section-title" }, el("h2", {}, "Audit trail")),
        table([
          { label: "when", render: (r) => when(r.at) },
          { label: "actor", render: (r) => el("code", {}, short(r.actor, 12)) },
          { label: "action", key: "action" },
          { label: "resource", render: (r) => `${r.resource_type}${r.resource_id ? `:${short(r.resource_id)}` : ""}` },
          { label: "result", render: (r) => badge(r.result === "success" ? "ok" : "fail") },
        ], audit.items));
    },
  },
};

// ───────────────────────────────────── detail renderers ───────────────────────────────────

async function renderDataset(name) {
  const [dataset, preview, schema] = await Promise.all([
    api(`/api/v1/datasets/${name}`), api(`/api/v1/datasets/${name}/preview?rows=20`), api(`/api/v1/datasets/${name}/schema`),
  ]);
  const columns = preview.columns.slice(0, 12).map((c) => ({ label: c.name, key: c.name }));
  return el("div", {},
    el("div", { class: "row" }, el("button", { class: "btn", onclick: () => go("datasets") }, "← catalog")),
    el("div", { class: "grid" },
      stat("Rows", num(dataset.row_count)),
      stat("Version", dataset.current_version),
      stat("Quality", dataset.quality_score ? dataset.quality_score.toFixed(1) : "—", "/100"),
      stat("Classification", dataset.classification)),
    el("div", { class: "row" },
      el("button", { class: "btn", onclick: () => act(`/api/v1/datasets/${name}/profile`, "profiled") }, "Profile"),
      el("button", { class: "btn", onclick: () => act(`/api/v1/datasets/${name}/validate`, "validated", { auto_expectations: true }) }, "Validate"),
      el("button", { class: "btn", onclick: () => act("/api/v1/reports", "report generated", { dataset: name, formats: ["html"] }) }, "Report")),
    el("div", { class: "section-title" }, el("h2", {}, "Schema")),
    table([
      { label: "column", key: "name" },
      { label: "type", render: (r) => el("code", {}, r.dtype) },
      { label: "meaning", render: (r) => badge(r.semantic_type) },
      { label: "classification", render: (r) => badge(r.classification.toLowerCase()) },
      { label: "nullable", render: (r) => (r.nullable ? "yes" : "no") },
    ], schema.columns),
    el("div", { class: "section-title" }, el("h2", {}, `Preview (${preview.masked ? "masked" : "raw"})`)),
    table(columns, preview.records));
}

async function renderJob(jobId) {
  const job = await api(`/api/v1/jobs/${jobId}`);
  const actions = el("div", { class: "row" },
    el("button", { class: "btn", onclick: () => go("jobs") }, "← jobs"),
    job.state === "AWAITING_APPROVAL" ? el("button", { class: "btn primary", onclick: () => act(`/api/v1/jobs/${jobId}/approve`, "approved", { steps: [] }) }, "Approve") : null,
    job.state === "FAILED" ? el("button", { class: "btn", onclick: () => act(`/api/v1/jobs/${jobId}/retry`, "re-queued") }, "Retry") : null,
    ["PENDING", "RETRYING"].includes(job.state) ? el("button", { class: "btn primary", onclick: () => act(`/api/v1/jobs/${jobId}/execute`, "executed") }, "Run now") : null);

  return el("div", {}, actions,
    el("div", { class: "grid" },
      stat("Pipeline", job.pipeline),
      stat("State", badge(job.state)),
      stat("Attempt", `${job.attempt}/${job.max_attempts}`),
      stat("Duration", (job.metrics || {}).duration_seconds ? `${job.metrics.duration_seconds.toFixed(2)}s` : "—")),
    job.error ? card("Error", el("div", {}, job.error), el("code", {}, job.error_code)) : null,
    el("div", { class: "section-title" }, el("h2", {}, "Steps")),
    table([
      { label: "#", key: "index", numeric: true },
      { label: "step", key: "step_id" },
      { label: "uses", render: (r) => el("code", {}, r.uses) },
      { label: "state", render: (r) => badge(r.state) },
      { label: "rows out", numeric: true, render: (r) => num(r.rows_out) },
      { label: "detail", render: (r) => (r.metrics || {}).message || r.error || "—" },
    ], job.steps || []),
    (job.insights || []).length ? el("div", {},
      el("div", { class: "section-title" }, el("h2", {}, "Insights")),
      ...job.insights.slice(0, 10).map(insight)) : null,
    (job.artifacts || []).length ? card("Artifacts", el("pre", { class: "json" }, job.artifacts.join("\n"))) : null);
}

async function act(path, message, body) {
  toast("working…");
  try {
    await api(path, { method: "POST", body: body || {} });
    toast(message);
    render();
  } catch (error) { toast(error.message, true); }
}

// ─────────────────────────────────────────── shell ────────────────────────────────────────

const NAV = [
  ["dashboard", "◧ Dashboard"], ["sources", "⚯ Sources"], ["datasets", "▤ Datasets"],
  ["pipelines", "⇶ Pipelines"], ["jobs", "◷ Jobs"], ["reports", "▦ Reports"],
  ["ai", "✦ AI Analyst"], ["governance", "⚖ Governance"],
];

let currentParams = {};

function go(page, params = {}) {
  state.page = page;
  currentParams = params;
  const query = new URLSearchParams({ page, ...params }).toString();
  history.replaceState(null, "", `#${query}`);
  render();
}

async function render() {
  const page = pages[state.page] || pages.dashboard;
  document.getElementById("page-title").textContent = page.title;
  document.getElementById("page-subtitle").textContent = page.subtitle;
  for (const button of document.querySelectorAll("#nav button")) {
    button.classList.toggle("active", button.dataset.page === state.page);
  }
  const view = document.getElementById("view");
  view.replaceChildren(el("div", { class: "loading" }, "loading…"));
  try {
    view.replaceChildren(await page.render(currentParams));
  } catch (error) {
    view.replaceChildren(el("div", { class: "empty" },
      el("p", {}, error.message),
      error.code ? el("code", {}, `${error.code}${error.trace ? ` · trace ${error.trace}` : ""}`) : null,
      error.code === "GDAP-3000" ? el("p", { class: "muted" }, "set an API key in the top bar") : null));
  }
}

async function boot() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...NAV.map(([page, label]) =>
    el("button", { "data-page": page, onclick: () => go(page) }, label)));

  const keyInput = document.getElementById("api-key");
  keyInput.value = state.apiKey;
  keyInput.addEventListener("change", () => {
    state.apiKey = keyInput.value.trim();
    localStorage.setItem("gdap.apiKey", state.apiKey);
    render();
  });

  document.getElementById("refresh").addEventListener("click", () => render());
  document.getElementById("theme").addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("gdap.theme", next);
  });
  const savedTheme = localStorage.getItem("gdap.theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;

  try {
    state.info = await api("/api/v1/system/info");
    document.getElementById("env-label").textContent = `${state.info.environment} · v${state.info.version}`;
  } catch { document.getElementById("env-label").textContent = "offline"; }

  try {
    const health = await api("/health");
    const pill = document.getElementById("health-pill");
    pill.className = `pill ${health.ok ? "ok" : "bad"}`;
    pill.textContent = health.ok ? "● all systems ok" : "● degraded";
  } catch { /* health pill stays neutral */ }

  const params = new URLSearchParams(location.hash.slice(1));
  const page = params.get("page");
  if (page && pages[page]) {
    state.page = page;
    currentParams = Object.fromEntries([...params.entries()].filter(([key]) => key !== "page"));
  }
  render();
}

boot();
