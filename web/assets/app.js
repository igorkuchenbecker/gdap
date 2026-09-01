/**
 * GDAP web UI.
 *
 * A deliberate design decision (ADR-007): no build step, no framework. The UI is a *client of the
 * public API* — the same endpoints the CLI and any third-party integration use — so it must never
 * become the place where behaviour lives. If a screen needs something the API cannot answer, the
 * fix belongs in the API.
 */

const state = {
  page: "dashboard",
  apiKey: localStorage.getItem("gdap.apiKey") || "",
  info: null,
};

// ────────────────────────────────────────── api client ────────────────────────────────────

async function api(path, { method = "GET", body } = {}) {
  const isForm = body instanceof FormData;
  const headers = {};
  if (body && !isForm) headers["Content-Type"] = "application/json";
  if (state.apiKey) headers["X-API-Key"] = state.apiKey;
  const response = await fetch(path, {
    method,
    headers,
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
  });
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
const bytes = (value) => {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
};

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

/**
 * A labelled proportion bar.
 *
 * The written value is the message and the bar is the comparison; the bar is never the only
 * thing that carries the number, so this still reads on a monochrome screen or to a reader
 * who cannot separate the hues. `tone` only ever emphasises what the label already says.
 */
function meter(label, ratio, text, tone = "") {
  const pct = Math.max(0, Math.min(1, Number(ratio) || 0)) * 100;
  return el("div", { class: "meter" },
    el("div", { class: "meter-head" }, el("span", {}, label), el("strong", {}, text)),
    el("div", { class: "meter-track" }, el("div", { class: `meter-fill ${tone}`, style: `width:${pct.toFixed(2)}%` })));
}

/**
 * Frequency distribution for one column, from the profiler's `top_values`.
 *
 * Bars are scaled to the most frequent value present rather than to the row count, so a column
 * whose values are all rare is still readable. The share each value represents is printed, because
 * "longest bar" answers a different question from "how much of the data is this".
 *
 * An empty list is not the same claim as "nothing repeats". The profiler skips `top_values`
 * entirely for free text and JSON blobs, where counting by exact value is meaningless, so the
 * empty state has to say *that* — reporting "no repeated values" for a column where values
 * plainly do repeat would be the panel inventing a finding.
 */
const _UNCOUNTED = new Set(["free_text", "json_blob"]);

function distribution(topValues, total, semanticType) {
  const rows = (topValues || []).slice(0, 8);
  if (!rows.length) {
    return el("div", { class: "empty" }, _UNCOUNTED.has(semanticType)
      ? `not summarised — ${String(semanticType).replace("_", " ")} is not counted by exact value`
      : "no values to summarise");
  }
  const largest = Math.max(...rows.map(([, count]) => count)) || 1;
  return el("div", { class: "dist" }, rows.map(([value, count]) => {
    const share = total ? (count / total) * 100 : 0;
    return el("div", { class: "dist-row" },
      el("span", { class: "dist-label", title: String(value) }, value === null || value === "" ? "(empty)" : String(value)),
      el("span", { class: "dist-track" }, el("span", { class: "dist-fill", style: `width:${((count / largest) * 100).toFixed(2)}%` })),
      el("span", { class: "dist-value" }, `${num(count)} · ${share.toFixed(1)}%`));
  }));
}
function stat(title, value, sub) {
  return el("div", { class: "card stat-card" }, el("h3", {}, title),
    el("div", { class: "stat" }, value, sub ? el("small", {}, sub) : null));
}

function sectionTitle(title, description, action) {
  return el("div", { class: "section-title" },
    el("div", {}, el("h2", {}, title), description ? el("p", {}, description) : null),
    action || null);
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

function uploadStudio({ compact = false } = {}) {
  let selectedFile = null;
  const fileInput = el("input", {
    type: "file",
    name: "file",
    accept: ".csv,.tsv,.json,.ndjson,.jsonl,.parquet,.pq,.xlsx,.xls",
    required: "true",
  });
  const dropContent = el("div", {},
    el("div", { class: "drop-glyph", "aria-hidden": "true" }, "↥"),
    el("strong", {}, "Drop a data file here"),
    el("p", {}, "or click to browse · CSV, JSON, Parquet or Excel"));
  const dropzone = el("label", { class: "dropzone" }, fileInput, dropContent);
  const submit = el("button", { class: "btn primary", type: "submit", disabled: "true" }, "Import dataset");
  const progress = el("div", { class: "upload-progress", hidden: "true" }, el("span"));

  function setFile(file) {
    if (!file) return;
    selectedFile = file;
    submit.disabled = false;
    dropContent.replaceChildren(
      el("div", { class: "drop-glyph", "aria-hidden": "true" }, "✓"),
      el("div", { class: "file-ready" },
        el("span", { class: "file-name" }, file.name),
        el("span", { class: "file-meta" }, `${bytes(file.size)} · ready to import`)),
      el("p", {}, "Click or drop another file to replace"));
  }

  fileInput.addEventListener("change", () => setFile(fileInput.files?.[0]));
  for (const eventName of ["dragenter", "dragover"]) {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    });
  }
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) setFile(file);
  });

  const form = el("form", {
    class: "upload-form",
    onsubmit: async (event) => {
      event.preventDefault();
      if (!selectedFile) return;
      const fields = new FormData(event.target);
      fields.set("file", selectedFile, selectedFile.name);
      if (!fields.get("dataset")) fields.delete("dataset");
      if (!fields.get("source")) fields.delete("source");
      submit.disabled = true;
      submit.textContent = "Importing…";
      progress.hidden = false;
      try {
        const result = await api("/api/v1/sources/upload", { method: "POST", body: fields });
        const dataset = result.result?.dataset || fields.get("dataset") || selectedFile.name.replace(/\.[^.]+$/, "");
        toast(`${dataset} is ready to explore`);
        go("datasets", { dataset });
      } catch (error) {
        toast(error.message, true);
        submit.disabled = false;
        submit.textContent = "Import dataset";
        progress.hidden = true;
      }
    },
  },
    dropzone,
    el("div", { class: "upload-fields" },
      el("div", { class: "field" },
        el("label", { for: compact ? "quick-dataset" : "source-dataset" }, "Dataset name · optional"),
        el("input", { id: compact ? "quick-dataset" : "source-dataset", type: "text", name: "dataset", placeholder: "e.g. monthly_revenue", pattern: "[A-Za-z0-9_-]+" }),
        el("span", { class: "field-note" }, "Leave blank to derive it from the filename.")),
      el("div", { class: "field" },
        el("label", { for: compact ? "quick-source" : "source-name" }, "Source name · optional"),
        el("input", { id: compact ? "quick-source" : "source-name", type: "text", name: "source", placeholder: "e.g. finance_uploads", pattern: "[A-Za-z0-9_-]+" })),
      el("div", { class: "row" }, submit, el("span", { class: "field-note" }, "Stored in your governed workspace")),
      progress));

  return el("div", { class: "card upload-card", id: compact ? "quick-import" : "import-data" },
    el("div", { class: "upload-head" },
      el("div", {}, el("h3", {}, "Import a file"), el("p", {}, "Upload, register and ingest in one step.")),
      el("span", { class: "eyebrow" }, "01 / CAPTURE")),
    form);
}

// ─────────────────────────────────────────── pages ────────────────────────────────────────

const pages = {
  dashboard: {
    kicker: "CONTROL ROOM", title: "Workspace", subtitle: "From raw files to decisions, with every step visible",
    async render() {
      const data = await api("/api/v1/system/dashboard");
      const quality = data.quality.average;
      const hasData = data.counts.datasets > 0;
      return el("div", { class: "page-stack" },
        el("section", { class: "mission" },
          el("div", { class: "mission-copy" },
            el("span", { class: "eyebrow" }, hasData ? "YOUR DATA SYSTEM IS LIVE" : "START WITH A SINGLE FILE"),
            el("h2", {}, "Raw data in. ", el("em", {}, "Clarity out.")),
            el("p", {}, "GDAP turns scattered files and sources into versioned, quality-checked datasets you can inspect, automate and ask questions about."),
            el("div", { class: "mission-actions" },
              el("button", { class: "btn primary", onclick: () => document.getElementById("quick-import")?.scrollIntoView({ behavior: "smooth" }) }, "Import your first file"),
              el("button", { class: "btn ghost", onclick: () => go("ai") }, "Ask the analyst →"))),
          el("div", { class: "data-path", "aria-label": "Data workflow" },
            el("div", { class: "path-node active" }, el("span", { class: "path-index" }, "01"), el("div", {}, el("strong", {}, "Capture"), el("small", {}, `${num(data.counts.sources)} connected sources`)), el("span", { class: "path-state" }, "READY")),
            el("div", { class: `path-node${hasData ? " active" : ""}` }, el("span", { class: "path-index" }, "02"), el("div", {}, el("strong", {}, "Govern"), el("small", {}, quality === null ? "quality awaits data" : `${quality.toFixed(1)} average quality`)), el("span", { class: "path-state" }, hasData ? "LIVE" : "NEXT")),
            el("div", { class: `path-node${data.counts.pipelines > 0 ? " active" : ""}` }, el("span", { class: "path-index" }, "03"), el("div", {}, el("strong", {}, "Activate"), el("small", {}, `${num(data.counts.pipelines)} automated pipelines`)), el("span", { class: "path-state" }, data.counts.pipelines > 0 ? "LIVE" : "PLAN")))),
        el("div", { class: "grid" },
          stat("Rows managed", num(data.counts.rows_total), "ACROSS EVERY VERSION"),
          stat("Datasets", num(data.counts.datasets), "READY TO QUERY"),
          stat("Quality", quality === null ? "—" : quality.toFixed(1), "OUT OF 100"),
          stat("Open alerts", num(data.counts.alerts_open), data.counts.alerts_open ? "NEEDS ATTENTION" : "ALL CLEAR")),
        uploadStudio({ compact: true }),
        el("div", { class: "split" },
          el("div", { class: "page-stack" },
            sectionTitle("Recent activity", "Every run stays traceable", el("button", { class: "btn small ghost", onclick: () => go("jobs") }, "View all")),
            table([
              { label: "job", render: (r) => el("code", {}, short(r.id)) },
              { label: "pipeline", key: "pipeline" },
              { label: "state", render: (r) => badge(r.state) },
              { label: "duration", numeric: true, render: (r) => (r.duration_seconds ? `${r.duration_seconds.toFixed(2)}s` : "—") },
              { label: "created", render: (r) => when(r.created_at) },
            ], data.recent_jobs, { onRow: (r) => go("jobs", { job: r.id }) })),
          el("div", { class: "page-stack" },
            sectionTitle("Attention", "Quality and governance signals"),
            data.open_alerts.length ? table([
              { label: "severity", render: (r) => badge(r.severity) },
              { label: "alert", key: "title" },
            ], data.open_alerts) : el("div", { class: "callout" }, "No open alerts. Your governed assets are operating normally."))),
        sectionTitle("Data catalog", "Your analysis-ready assets", el("button", { class: "btn small ghost", onclick: () => go("datasets") }, "Open catalog")),
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
    kicker: "CAPTURE", title: "Sources", subtitle: "Bring files and systems into one governed workspace",
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
          toast("Source registered");
          go("sources");
        } catch (error) { toast(`${error.message}`, true); }
      }},
        el("label", {}, "name"), el("input", { type: "text", name: "name", required: "true", placeholder: "sales_files" }),
        el("label", {}, "connector"),
        el("select", { name: "connector" }, connectors.items.map((c) => el("option", { value: c.key }, `${c.key} — ${c.title}`))),
        el("label", {}, "config (JSON)"),
        el("textarea", { name: "config", placeholder: '{"path": "/data/sales", "pattern": "*.csv"}' }),
        el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit" }, "Register source")));

      return el("div", { class: "page-stack" },
        uploadStudio(),
        sectionTitle("Connected sources", `${items.length} registered connection${items.length === 1 ? "" : "s"}`),
        table([
          { label: "source", render: (r) => el("div", { class: "source-type" }, el("span", {}, "↳"), el("strong", {}, r.name)) },
          { label: "connector", render: (r) => el("code", {}, r.connector) },
          { label: "status", render: (r) => badge(r.status) },
          { label: "classification", render: (r) => badge(r.classification.toLowerCase()) },
          { label: "last tested", render: (r) => when(r.last_tested_at) },
          { label: "", render: (r) => el("button", { class: "btn", onclick: async (event) => {
              event.stopPropagation();
              try { const result = await api(`/api/v1/sources/${r.name}/test`, { method: "POST" });
                toast(result.ok ? `${r.name}: ${result.message}` : `${r.name}: ${result.message}`, !result.ok);
              } catch (error) { toast(error.message, true); }
            }}, "Test") },
        ], items),
        sectionTitle("Advanced connection", "Databases, APIs and watched directories"),
        card("Register a source manually", form));
    },
  },

  datasets: {
    kicker: "GOVERN", title: "Datasets", subtitle: "Versioned assets with quality, schema and lineage",
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
    kicker: "AUTOMATE", title: "Pipelines", subtitle: "Repeatable data work, declared and reviewable",
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
    kicker: "OBSERVE", title: "Jobs", subtitle: "Every run, step and outcome in one timeline",
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
    kicker: "DELIVER", title: "Reports", subtitle: "Decision-ready artifacts generated from governed data",
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
    kicker: "UNDERSTAND", title: "AI Analyst", subtitle: "Evidence-backed answers, grounded in your datasets",
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
    kicker: "TRUST", title: "Governance", subtitle: "Lineage, audit and classification without the paperwork",
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

/**
 * What the profiler found, rendered.
 *
 * The platform already computes all of this — distributions, null and distinct ratios, candidate
 * keys, correlations, recommendations — and until now the UI could *trigger* a profile with the
 * button above and had nowhere to show the result. Everything here comes from the one
 * `GET /datasets/{name}/profile` response; no endpoint was added for it.
 */
/**
 * Unique column pairs from the profiler's correlation matrix, strongest first.
 *
 * The payload is a full matrix (`{a: {a: 1, b: .81}, b: {...}}`), so it carries every pair twice
 * plus each column's correlation with itself. Both are dropped here: "revenue correlates with
 * revenue at 1.0" is not a finding, and showing a pair twice would overstate how much the
 * profiler found. Sorted by magnitude because a strong negative correlation is as interesting
 * as a strong positive one.
 */
function correlationPairs(matrix) {
  if (!matrix || typeof matrix !== "object") return [];
  const seen = new Set();
  const pairs = [];
  for (const [left, row] of Object.entries(matrix)) {
    for (const [right, value] of Object.entries(row || {})) {
      if (left === right || !Number.isFinite(value)) continue;
      const key = [left, right].sort().join(" ");
      if (seen.has(key)) continue;
      seen.add(key);
      pairs.push({ left, right, value });
    }
  }
  return pairs.sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
}

function profileSection(name, profile) {
  if (!profile) {
    return el("div", { class: "empty" },
      "no profile yet — press Profile above to have the platform read this dataset's shape");
  }

  const columns = profile.column_profiles || [];
  const dupRatio = Number(profile.duplicate_ratio) || 0;
  const keys = profile.candidate_keys || [];
  const pairs = correlationPairs(profile.correlations);
  const strongest = pairs[0];

  return el("div", {},
    el("div", { class: "grid" },
      stat("Duplicate rows", num(profile.duplicate_rows), `${(dupRatio * 100).toFixed(2)}% OF ROWS`),
      stat("Candidate keys", keys.length ? keys.join(", ") : "none", keys.length ? "UNIQUE ACROSS EVERY ROW" : "NO COLUMN IDENTIFIES A ROW"),
      // The strongest pair, not a count of pairs: "6 correlations" tells you nothing you can act
      // on, and every matrix of n numeric columns has the same n(n-1)/2 of them.
      stat("Strongest link", strongest ? strongest.value.toFixed(2) : "—",
        strongest ? `${strongest.left} ↔ ${strongest.right}`.toUpperCase() : "NO NUMERIC PAIRS"),
      // Sampling changes what every number above is a claim about, so it is stated, not implied.
      stat("Profiled", profile.sampled ? `${num(profile.sample_rows)} rows` : "every row", profile.sampled ? "SAMPLED — FIGURES ARE ESTIMATES" : "FULL SCAN")),

    (profile.recommendations || []).length
      ? el("div", {},
          el("div", { class: "section-title" }, el("h2", {}, "What the profiler suggests"),
            el("p", {}, "Suggestions, not changes — nothing here has been applied")),
          el("div", { class: "rec-list" }, profile.recommendations.map((r) =>
            el("div", { class: "callout" }, typeof r === "string" ? r : (r.message || JSON.stringify(r))))))
      : null,

    el("div", { class: "section-title" }, el("h2", {}, "Columns"),
      el("p", {}, `${columns.length} profiled · bars are scaled within each column`)),
    el("div", { class: "profile-grid" }, columns.map((column) => {
      const nulls = Number(column.null_ratio) || 0;
      const distinct = Number(column.distinct_ratio) || 0;
      return el("div", { class: "card profile-card" },
        el("div", { class: "profile-head" },
          el("strong", {}, column.name),
          el("code", {}, column.dtype),
          badge(column.semantic_type),
          column.classification ? badge(String(column.classification).toLowerCase()) : null),
        meter("complete", 1 - nulls, nulls === 0 ? "no nulls" : `${((1 - nulls) * 100).toFixed(1)}% · ${num(column.null_count)} null`, nulls > 0.2 ? "warn" : ""),
        meter("distinct", distinct, `${num(column.distinct_count)} value${column.distinct_count === 1 ? "" : "s"}`),
        column.is_constant ? el("p", { class: "field-note" }, "constant — every row holds the same value") : null,
        column.is_unique ? el("p", { class: "field-note" }, "unique — no value repeats") : null,
        distribution(column.top_values, column.count, column.semantic_type));
    })));
}

async function renderDataset(name) {
  const [dataset, preview, schema, profile] = await Promise.all([
    api(`/api/v1/datasets/${name}`), api(`/api/v1/datasets/${name}/preview?rows=20`), api(`/api/v1/datasets/${name}/schema`),
    // A dataset that was never profiled answers 200 with null rather than 404, so a failure here
    // is a real one and still surfaces; only "nothing computed yet" is treated as an empty state.
    api(`/api/v1/datasets/${name}/profile`).catch(() => null),
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
    table(columns, preview.records),
    el("div", { class: "section-title" }, el("h2", {}, "Profile"),
      el("p", {}, "How this data is actually shaped, measured by the platform")),
    profileSection(name, profile));
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
  ["dashboard", "⌂", "Workspace", "01"], ["sources", "↳", "Sources", "02"],
  ["datasets", "▤", "Datasets", "03"], ["pipelines", "⇶", "Pipelines", "04"],
  ["jobs", "◷", "Jobs", "05"], ["reports", "▦", "Reports", "06"],
  ["ai", "✦", "AI Analyst", "07"], ["governance", "◇", "Governance", "08"],
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
  document.getElementById("page-kicker").textContent = page.kicker || "GDAP";
  document.getElementById("page-title").textContent = page.title;
  document.getElementById("page-subtitle").textContent = page.subtitle;
  for (const button of document.querySelectorAll("#nav button")) {
    button.classList.toggle("active", button.dataset.page === state.page);
  }
  const view = document.getElementById("view");
  view.replaceChildren(el("div", { class: "loading" }, el("span"), "Loading workspace…"));
  try {
    const content = await page.render(currentParams);
    content.classList.add("page-stack");
    view.replaceChildren(content);
  } catch (error) {
    view.replaceChildren(el("div", { class: "empty" },
      el("p", {}, error.message),
      error.code ? el("code", {}, `${error.code}${error.trace ? ` · trace ${error.trace}` : ""}`) : null,
      error.code === "GDAP-3000" ? el("p", { class: "muted" }, "set an API key in the top bar") : null));
  }
}

async function boot() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...NAV.map(([page, icon, label, key]) =>
    el("button", { "data-page": page, onclick: () => go(page), title: label },
      el("span", { class: "nav-icon", "aria-hidden": "true" }, icon),
      el("span", {}, label),
      el("span", { class: "nav-key", "aria-hidden": "true" }, key))));

  document.querySelector(".brand").addEventListener("click", () => go("dashboard"));

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
    document.getElementById("env-label").textContent = `${state.info.environment} / v${state.info.version}`;
  } catch { document.getElementById("env-label").textContent = "offline"; }

  try {
    const health = await api("/health");
    const pill = document.getElementById("health-pill");
    pill.className = `health ${health.ok ? "ok" : "bad"}`;
    pill.replaceChildren(el("span"), health.ok ? "Platform operational" : "Platform degraded");
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
