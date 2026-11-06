(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const numberFormat = new Intl.NumberFormat("zh-CN");

  const safeStorage = {
    get(key) {
      try {
        return sessionStorage.getItem(key) || "";
      } catch {
        return "";
      }
    },
    set(key, value) {
      try {
        if (value) sessionStorage.setItem(key, value);
        else sessionStorage.removeItem(key);
      } catch {
        /* Session storage is optional. */
      }
    },
  };

  const demo = {
    capabilities: [
      {
        code: "inference.llm",
        name: "统一模型推理",
        type: "MODEL",
        status: "ACTIVE",
        owner: "模型平台组",
        description: "多模型路由、流式响应、Token 计量与失败回退。",
        tags: ["LLM", "SSE", "配额"],
        default_version: "v1.4.2",
        versions: [{ version: "v1.4.2", status: "PUBLISHED" }, { version: "v1.4.1", status: "PUBLISHED" }],
      },
      {
        code: "retrieval.legal",
        name: "公共法律检索",
        type: "RAG",
        status: "ACTIVE",
        owner: "知识工程组",
        description: "法条、案例、司法解释混合检索，支持引用溯源与上下文压缩。",
        tags: ["RAG", "引用", "压缩"],
        default_version: "v2.1.0",
        versions: [{ version: "v2.1.0", status: "PUBLISHED" }, { version: "v2.0.3", status: "PUBLISHED" }],
      },
      {
        code: "mcp.judicial",
        name: "司法工具集",
        type: "MCP",
        status: "ACTIVE",
        owner: "应用中台组",
        description: "通过 MCP 协议统一注册、发现与调用司法业务工具。",
        tags: ["MCP", "工具", "审计"],
        default_version: "v1.0.8",
        versions: [{ version: "v1.0.8", status: "PUBLISHED" }],
      },
      {
        code: "governance.loop",
        name: "数据治理闭环",
        type: "GOVERNANCE",
        status: "BETA",
        owner: "数据治理组",
        description: "事件采集、标注、评测、坏案例与数据版本闭环。",
        tags: ["评测", "标注", "保留"],
        default_version: "v0.9.6",
        versions: [{ version: "v0.9.6", status: "BETA" }],
      },
    ],
    kbs: [
      {
        id: 1,
        code: "kb_statutes",
        name: "法律法规库",
        domain: "STATUTE",
        security_level: "INTERNAL",
        status: "READY",
        owner: "知识工程组",
        description: "现行有效法律、行政法规与司法解释。",
        chunk_count: 18420,
      },
      {
        id: 2,
        code: "kb_cases",
        name: "指导性案例库",
        domain: "CASE",
        security_level: "CONFIDENTIAL",
        status: "READY",
        owner: "案例研究组",
        description: "最高人民法院指导性案例与公报案例。",
        chunk_count: 6230,
      },
      {
        id: 3,
        code: "kb_procedure",
        name: "诉讼程序指引",
        domain: "PROCEDURE",
        security_level: "INTERNAL",
        status: "INDEXING",
        owner: "审判管理组",
        description: "立案、庭审、执行等程序性规范与操作指引。",
        chunk_count: 1580,
      },
    ],
    metrics: {
      events: {
        total: 12847,
        by_type: {
          MODEL_INFERENCE: 6230,
          RETRIEVAL_COMPLETED: 4318,
          MCP_TOOL_CALL: 1620,
          FEEDBACK_SUBMITTED: 679,
        },
        by_status: { SUCCESS: 11904, FAILED: 96, NEW: 847 },
      },
      feedback: { total: 2184, positive: 1996, positive_rate: 0.914, coverage_rate: 0.17 },
      bad_cases: { total: 42, close_rate: 0.78, avg_close_hours: 18.4, by_status: { OPEN: 9, CLOSED: 33 } },
      annotations: { tasks: 12, records: 864, approval_rate: 0.92, agreement_rate: 0.914, cohen_kappa: 0.81 },
      datasets: {
        total: 8,
        versions: 21,
        published_versions: 14,
        items: 5602,
        avg_quality_score: 0.91,
        review_status: { APPROVED: 4320, PENDING: 120, REJECTED: 42 },
      },
    },
    events: [
      {
        event_type: "RETRIEVAL_COMPLETED",
        status: "SUCCESS",
        created_at: new Date(Date.now() - 45 * 1000).toISOString(),
        payload: { query: "民间借贷利率超过 LPR 四倍，法院是否支持？", result_count: 8, compression: { ratio: 0.59 } },
      },
      {
        event_type: "MODEL_INFERENCE",
        status: "SUCCESS",
        created_at: new Date(Date.now() - 2 * 60 * 1000).toISOString(),
        payload: { model: "qwen-judicial", total_tokens: 1284, latency_ms: 462 },
      },
      {
        event_type: "EVAL_COMPLETED",
        status: "NEW",
        created_at: new Date(Date.now() - 9 * 60 * 1000).toISOString(),
        payload: { task_id: "eval_20261020_03", low_score_count: 12 },
      },
      {
        event_type: "MCP_TOOL_CALL",
        status: "SUCCESS",
        created_at: new Date(Date.now() - 16 * 60 * 1000).toISOString(),
        payload: { tool: "case.extract_elements", result: "ok" },
      },
      {
        event_type: "ANNOTATION_SUBMITTED",
        status: "PROCESSED",
        created_at: new Date(Date.now() - 28 * 60 * 1000).toISOString(),
        payload: { task_id: "task_021", record_count: 5 },
      },
      {
        event_type: "FEEDBACK_SUBMITTED",
        status: "SUCCESS",
        created_at: new Date(Date.now() - 35 * 60 * 1000).toISOString(),
        payload: { rating: "NEGATIVE", reason: "引用不完整" },
      },
    ],
  };

  const state = {
    route: "overview",
    adminToken: safeStorage.get("jai.adminToken"),
    appToken: safeStorage.get("jai.appToken"),
    adminConnected: false,
    appConnected: false,
    environment: "unknown",
    demoEnabled: false,
    capabilities: null,
    kbs: null,
    metrics: null,
    events: null,
    apps: null,
    tenants: null,
    datasets: null,
    datasetVersions: new Map(),
    activeCapabilityCode: "",
    selectedKbIds: new Set([1, 2]),
    eventFilter: "",
    commandResults: [],
    commandIndex: 0,
    toastTimer: null,
  };

  const routes = {
    overview: { title: "总览", label: "运行总览" },
    capabilities: { title: "能力网关", label: "能力网关" },
    retrieval: { title: "知识检索", label: "知识检索" },
    governance: { title: "数据治理", label: "数据治理" },
    settings: { title: "系统设置", label: "系统设置" },
  };

  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (character) => {
      const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
      return map[character];
    });

  const formatNumber = (value) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numberFormat.format(numeric) : String(value ?? "—");
  };

  const formatPercent = (value, digits = 1) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "—";
    return `${(numeric * 100).toFixed(digits)}%`;
  };

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const truncate = (value, length = 80) => {
    const text = String(value ?? "");
    return text.length > length ? `${text.slice(0, length)}…` : text;
  };

  const relativeTime = (value) => {
    if (!value) return "刚刚";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function api(path, options = {}) {
    const { token = state.adminToken, method = "GET", body } = options;
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message =
        payload?.error?.message ||
        payload?.detail ||
        payload?.message ||
        `请求失败（HTTP ${response.status}）`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function sparkline(values) {
    const numbers = values.map(Number).filter(Number.isFinite);
    if (numbers.length < 2) return "";
    const width = 100;
    const height = 30;
    const padding = 2;
    const min = Math.min(...numbers);
    const max = Math.max(...numbers);
    const span = max - min || 1;
    const step = (width - padding * 2) / (numbers.length - 1);
    const points = numbers.map((value, index) => {
      const x = padding + index * step;
      const y = padding + (1 - (value - min) / span) * (height - padding * 2);
      return [x, y];
    });
    const line = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
    const area = `${line} L${points.at(-1)[0].toFixed(2)} ${height} L${points[0][0].toFixed(2)} ${height} Z`;
    const last = points.at(-1);
    return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
      <path d="${area}" fill="currentColor" opacity=".08"></path>
      <path d="${line}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"></path>
      <circle cx="${last[0].toFixed(2)}" cy="${last[1].toFixed(2)}" r="2" fill="currentColor" vector-effect="non-scaling-stroke"></circle>
    </svg>`;
  }

  function renderSparks() {
    $$(".spark[data-spark]").forEach((node) => {
      const values = node.dataset.spark.split(",").map(Number);
      node.innerHTML = sparkline(values);
    });
  }

  function currentCapabilities() {
    return state.capabilities ?? (state.demoEnabled ? demo.capabilities : []);
  }

  function currentKbs() {
    return state.kbs ?? (state.demoEnabled ? demo.kbs : []);
  }

  function currentMetrics() {
    return state.metrics ?? (state.demoEnabled ? demo.metrics : {});
  }

  function currentEvents() {
    return state.events ?? (state.demoEnabled ? demo.events : []);
  }

  function currentApps() {
    return state.apps ?? [];
  }

  function currentTenants() {
    return state.tenants ?? (state.demoEnabled ? [{ id: 1, code: "default", name: "默认租户" }] : []);
  }

  function currentDatasets() {
    return state.datasets ?? [];
  }

  function fillSelect(select, items, valueKey, labelFn, placeholder) {
    if (!select) return;
    const previous = select.value;
    const options = items.map(
      (item) =>
        `<option value="${escapeHtml(item[valueKey])}">${escapeHtml(labelFn(item))}</option>`,
    );
    select.innerHTML =
      (placeholder ? `<option value="">${escapeHtml(placeholder)}</option>` : "") + options.join("");
    if (options.some((option) => option.includes(`value="${previous}"`))) select.value = previous;
  }

  const typeMeta = {
    MODEL: { label: "模型", icon: "i-cpu", className: "is-blue" },
    RAG: { label: "检索", icon: "i-book", className: "is-gold" },
    GRAPH: { label: "图谱", icon: "i-layers", className: "is-teal" },
    MCP: { label: "工具", icon: "i-tool", className: "is-teal" },
    GOVERNANCE: { label: "治理", icon: "i-shield", className: "is-ink" },
  };

  function normalizeCapabilityType(type) {
    const value = String(type || "").toUpperCase();
    if (value.startsWith("MODEL")) return "MODEL";
    if (value.startsWith("GRAPH")) return "GRAPH";
    if (value.startsWith("RAG")) return "RAG";
    if (value.startsWith("MCP")) return "MCP";
    if (value.startsWith("GOVERNANCE")) return "GOVERNANCE";
    return value;
  }

  const typeLabel = (type) => typeMeta[normalizeCapabilityType(type)]?.label || type || "能力";
  const typeIcon = (type) => typeMeta[normalizeCapabilityType(type)]?.icon || "i-grid";
  const typeClass = (type) => typeMeta[normalizeCapabilityType(type)]?.className || "is-blue";

  const statusMeta = (status) => {
    const value = String(status || "").toUpperCase();
    if (["ACTIVE", "PUBLISHED", "READY", "SUCCESS", "PROCESSED", "COMPLETED"].includes(value)) {
      return { label: value === "PUBLISHED" ? "已发布" : value === "READY" ? "就绪" : "运行中", className: "is-ok" };
    }
    if (["BETA", "DRAFT", "INDEXING", "NEW", "PENDING", "PENDING_APPROVAL"].includes(value)) {
      return {
        label: value === "INDEXING" ? "索引中" : value === "NEW" ? "待处理" : "试运行",
        className: "is-warn",
      };
    }
    if (["FAILED", "ERROR", "DEAD", "DISABLED", "OFFLINE", "REJECTED"].includes(value)) {
      return { label: "异常", className: "is-rose" };
    }
    return { label: status || "未知", className: "" };
  };

  const eventStatusMeta = (status) => {
    const value = String(status || "").toUpperCase();
    if (["SUCCESS", "COMPLETED"].includes(value)) return { label: "成功", className: "is-ok" };
    if (["PROCESSED"].includes(value)) return { label: "已处理", className: "is-ok" };
    if (["NEW", "PENDING"].includes(value)) return { label: "待处理", className: "is-warn" };
    if (["FAILED", "ERROR"].includes(value)) return { label: "失败", className: "is-rose" };
    if (["DEAD", "DISCARDED"].includes(value)) return { label: "死信", className: "is-rose" };
    return { label: status || "未知", className: "" };
  };

  const intentLabels = {
    STATUTE: "法条检索",
    CASE: "案例分析",
    PROCEDURE: "程序咨询",
    TOOL: "工具调用",
    GENERAL: "通用咨询",
    CLARIFY: "需要澄清",
  };

  const intentLabel = (value) => intentLabels[String(value || "").toUpperCase()] || value || "通用咨询";

  const eventMeta = {
    MODEL_INFERENCE: { label: "模型推理", icon: "i-cpu", className: "is-blue" },
    INFERENCE_COMPLETED: { label: "模型推理", icon: "i-cpu", className: "is-blue" },
    RETRIEVAL_COMPLETED: { label: "检索完成", icon: "i-search", className: "is-blue" },
    MCP_TOOL_CALL: { label: "MCP 工具", icon: "i-tool", className: "is-teal" },
    FEEDBACK_SUBMITTED: { label: "反馈提交", icon: "i-spark", className: "is-gold" },
    EVAL_COMPLETED: { label: "评测任务", icon: "i-alert", className: "is-gold" },
    ANNOTATION_SUBMITTED: { label: "标注提交", icon: "i-check", className: "is-teal" },
    ANNOTATION_RECORD_SUBMITTED: { label: "标注提交", icon: "i-check", className: "is-teal" },
    ANNOTATION_COMPLETED: { label: "标注提交", icon: "i-check", className: "is-teal" },
    TOOL_CALLED: { label: "MCP 工具", icon: "i-tool", className: "is-teal" },
    DATASET_VERSION_PUBLISHED: { label: "数据集发布", icon: "i-database", className: "is-gold" },
    RETENTION_COMPLETED: { label: "保留任务", icon: "i-database", className: "is-gold" },
  };

  function getEventMeta(type) {
    return eventMeta[String(type || "").toUpperCase()] || { label: type || "平台事件", icon: "i-activity", className: "is-blue" };
  }

  function eventDetail(event) {
    const payload = event.payload || {};
    if (payload.query) return truncate(payload.query, 56);
    if (payload.tool) return `工具 ${payload.tool}`;
    if (payload.model) {
      const tokens = payload.total_tokens ? ` · ${formatNumber(payload.total_tokens)} tokens` : "";
      return `${payload.model}${tokens}`;
    }
    if (payload.task_id) return `任务 ${payload.task_id}`;
    if (payload.reason) return `原因：${payload.reason}`;
    return event.object_ref || event.request_id || "平台事件";
  }

  function renderStats() {
    const metrics = currentMetrics();
    const isDemo = !state.metrics;
    const events = metrics.events || {};
    const byType = events.by_type || {};
    const byStatus = events.by_status || {};
    const total = Number(events.total || 0);
    const success = Number(byStatus.SUCCESS || 0) + Number(byStatus.PROCESSED || 0) + Number(byStatus.COMPLETED || 0);
    const failed = Number(byStatus.FAILED || 0) + Number(byStatus.ERROR || 0) + Number(byStatus.DEAD || 0);
    const attempts = success + failed;
    const successRate = attempts ? clamp((success / attempts) * 100, 0, 100) : isDemo ? 99.2 : 0;
    const feedbackRate = Number(metrics.feedback?.positive_rate || 0);
    const qualityScore = Number(metrics.datasets?.avg_quality_score || 0) * 100;

    $("#statCalls").textContent = formatNumber(isDemo ? 12847 : total);
    $("#statFeedback").textContent = isDemo ? "91.4" : (feedbackRate * 100).toFixed(1);
    $("#statFeedbackSub").textContent = isDemo
      ? "1,996 / 2,184"
      : `${formatNumber(metrics.feedback?.positive || 0)} / ${formatNumber(metrics.feedback?.total || 0)}`;
    $("#statQuality").textContent = isDemo ? "91.0" : qualityScore.toFixed(1);
    $("#statQualitySub").textContent = isDemo
      ? "5,602 条数据"
      : `${formatNumber(metrics.datasets?.items || 0)} 条数据`;

    $("#availabilityValue").innerHTML = `${successRate.toFixed(1)}<span>%</span>`;
    $("#pulseGauge").style.setProperty("--value", successRate.toFixed(1));
    $("#pulseInference").textContent = formatNumber(isDemo ? 6230 : byType.MODEL_INFERENCE || byType.INFERENCE_COMPLETED || 0);
    $("#pulseRetrieval").textContent = formatNumber(isDemo ? 4318 : byType.RETRIEVAL_COMPLETED || 0);
    $("#pulseTools").textContent = formatNumber(isDemo ? 1620 : byType.MCP_TOOL_CALL || 0);

    const processedRate = total
      ? clamp(((total - Number(byStatus.NEW || 0)) / total) * 100, 0, 100)
      : isDemo
        ? 98.7
        : 0;
    const agreement = isDemo ? 91.4 : Number(metrics.annotations?.agreement_rate || 0) * 100;
    const closeRate = isDemo ? 78 : Number(metrics.bad_cases?.close_rate || 0) * 100;
    const healthScore = Math.round((processedRate + agreement + closeRate) / 3);

    $("#healthEvents").textContent = `${processedRate.toFixed(1)}%`;
    $("#healthAgreement").textContent = `${agreement.toFixed(1)}%`;
    $("#healthCases").textContent = `${closeRate.toFixed(1)}%`;
    $("#healthScore").textContent = String(healthScore);
    const healthRing = $(".health-card .score-ring");
    if (healthRing) healthRing.style.setProperty("--score", healthScore);

    const healthMeters = $$(".health-card .micro-meter i");
    if (healthMeters[0]) healthMeters[0].style.width = `${processedRate.toFixed(1)}%`;
    if (healthMeters[1]) healthMeters[1].style.width = `${agreement.toFixed(1)}%`;
    if (healthMeters[2]) healthMeters[2].style.width = `${closeRate.toFixed(1)}%`;
  }

  function renderCapabilityStrip() {
    const container = $("#capabilityStrip");
    if (!container) return;
    const items = currentCapabilities().slice(0, 4);
    if (!items.length) {
      container.innerHTML = `<div class="empty-state"><strong>暂无能力</strong><span>在管理接口注册能力后会显示在这里。</span></div>`;
      return;
    }
    container.innerHTML = items
      .map((item) => {
        const status = statusMeta(item.status);
        return `<button class="capability-mini" type="button" data-route="capabilities">
          <span class="cap-icon ${typeClass(item.type)}"><svg><use href="#${typeIcon(item.type)}"></use></svg></span>
          <span class="cap-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(truncate(item.description || item.code, 30))}</small></span>
          <span class="cap-state ${status.className === "is-warn" ? "is-beta" : ""}"><i></i>${escapeHtml(status.label)}</span>
        </button>`;
      })
      .join("");
  }

  function renderCapabilityTable() {
    const query = $("#capabilitySearch")?.value.trim().toLowerCase() || "";
    const type = $("#capabilityType")?.value || "";
    const allItems = currentCapabilities();
    const items = allItems.filter((item) => {
      const haystack = [item.name, item.code, item.owner, item.description].join(" ").toLowerCase();
      return (
        (!query || haystack.includes(query)) &&
        (!type || normalizeCapabilityType(item.type) === type)
      );
    });

    const rows = $("#capabilityRows");
    if (!rows) return;
    rows.innerHTML = items
      .map((item) => {
        const status = statusMeta(item.status);
        return `<tr data-capability-code="${escapeHtml(item.code)}">
          <td><div class="table-name"><span class="table-icon ${typeClass(item.type)}"><svg><use href="#${typeIcon(item.type)}"></use></svg></span><div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.code)}</span></div></div></td>
          <td><span class="type-pill">${escapeHtml(typeLabel(item.type))}</span></td>
          <td><span class="status-pill ${status.className}">${escapeHtml(status.label)}</span></td>
          <td><span class="table-version">${escapeHtml(item.default_version || item.versions?.[0]?.version || "—")}</span></td>
          <td><span class="table-owner">${escapeHtml(item.owner || "—")}</span></td>
          <td class="align-right"><button class="table-action" type="button" data-open-capability="${escapeHtml(item.code)}">详情<svg><use href="#i-chevron-right"></use></svg></button></td>
        </tr>`;
      })
      .join("");

    $("#capabilityEmpty").hidden = items.length > 0;
    $("#capabilityCount").textContent = `${items.length} 项能力`;
    $("#capTotal").textContent = String(allItems.length);
    $("#capActive").textContent = String(
      allItems.filter((item) =>
        ["ACTIVE", "PUBLISHED"].includes(String(item.status).toUpperCase()),
      ).length,
    );
  }

  function securityLabel(level) {
    const value = String(level || "INTERNAL").toUpperCase();
    return { PUBLIC: "公开", INTERNAL: "内部", CONFIDENTIAL: "机密", SECRET: "绝密" }[value] || value;
  }

  function kbStatus(status) {
    const value = String(status || "").toUpperCase();
    if (["READY", "ACTIVE", "PUBLISHED"].includes(value)) return "is-ok";
    if (["INDEXING", "PENDING", "DRAFT"].includes(value)) return "is-warn";
    return "is-rose";
  }

  function renderKbs() {
    const kbs = currentKbs();
    const list = $("#kbList");
    const chips = $("#kbChips");
    if (!list || !chips) return;

    const validIds = new Set(kbs.map((item) => Number(item.id)));
    const selected = [...state.selectedKbIds].filter((id) => validIds.has(id));
    if (!selected.length && kbs.length) {
      state.selectedKbIds = new Set(kbs.slice(0, 2).map((item) => Number(item.id)));
    }

    $("#kbCount").textContent = String(kbs.length);
    if (!kbs.length) {
      list.innerHTML = `<div class="empty-state"><strong>暂无知识库</strong><span>创建知识库并完成索引后会显示在这里。</span></div>`;
      chips.innerHTML = "";
      return;
    }

    list.innerHTML = kbs
      .map((item, index) => {
        const iconClass = index % 3 === 1 ? "is-gold" : index % 3 === 2 ? "is-teal" : "";
        const chunkText =
          item.chunk_count != null
            ? `${formatNumber(item.chunk_count)} 个切片`
            : "待统计切片";
        const docText =
          item.document_count != null ? ` · ${formatNumber(item.document_count)} 文档` : "";
        return `<div class="kb-item">
          <span class="kb-icon ${iconClass}"><svg><use href="#i-book"></use></svg></span>
          <div><strong>${escapeHtml(item.name)}</strong><span>${chunkText}${docText} · ${escapeHtml(securityLabel(item.security_level))}</span></div>
          <i class="status-dot ${kbStatus(item.status)}"></i>
        </div>`;
      })
      .join("");

    chips.innerHTML = kbs
      .map(
        (item) =>
          `<button class="chip ${state.selectedKbIds.has(Number(item.id)) ? "is-selected" : ""}" type="button" data-kb-id="${escapeHtml(item.id)}"><i></i>${escapeHtml(item.name)}</button>`,
      )
      .join("");
  }

  function eventRow(event) {
    const meta = getEventMeta(event.event_type);
    return `<div class="event-item">
      <span class="event-icon ${meta.className}"><svg><use href="#${meta.icon}"></use></svg></span>
      <div><strong>${escapeHtml(meta.label)} · ${escapeHtml(eventDetail(event))}</strong><span>${escapeHtml(event.request_id || event.object_ref || "审计事件")} · ${escapeHtml(event.status || "UNKNOWN")}</span></div>
      <time>${escapeHtml(relativeTime(event.created_at))}</time>
    </div>`;
  }

  function renderHomeEvents() {
    const list = $("#homeEventList");
    if (!list) return;
    const events = currentEvents().slice(0, 4);
    list.innerHTML = events.length
      ? events.map(eventRow).join("")
      : `<div class="empty-state"><strong>暂无事件</strong><span>平台调用后，事件流会实时显示在这里。</span></div>`;
  }

  function renderGovernance() {
    const metrics = currentMetrics();
    const events = metrics.events || {};
    const feedback = metrics.feedback || {};
    const badCases = metrics.bad_cases || {};
    const annotations = metrics.annotations || {};
    const datasets = metrics.datasets || {};

    $("#govEvents").textContent = formatNumber(events.total || 0);
    $("#govFeedback").textContent = formatPercent(feedback.positive_rate || 0);
    $("#govAgreement").textContent = formatPercent(annotations.agreement_rate || 0);
    $("#govCases").textContent = formatPercent(badCases.close_rate || 0);

    $("#qualityTotal").textContent = formatNumber(datasets.items || 0);
    $("#feedbackRate").textContent = ((Number(feedback.positive_rate) || 0) * 100).toFixed(1);

    const review = datasets.review_status || {};
    const bars = $$(".quality-bars > div");
    const reviewItems = [
      { value: Number(review.APPROVED || 0), className: "" },
      { value: Number(review.PENDING || 0), className: "is-gold" },
      { value: Number(review.REJECTED || 0), className: "is-rose" },
    ];
    const reviewTotal = reviewItems.reduce((total, item) => total + item.value, 0);
    bars.forEach((bar, index) => {
      const item = reviewItems[index];
      if (!item) return;
      const width = reviewTotal && item.value ? clamp((item.value / reviewTotal) * 100, 0.5, 100) : 0;
      const fill = bar.querySelector("i");
      if (fill) fill.style.width = `${width}%`;
      const count = bar.querySelector("strong");
      if (count) count.textContent = formatNumber(item.value);
    });

    const feedbackBars = $$(".feedback-bars > div");
    if (feedbackBars[0]) {
      const positive = Number(feedback.positive || 0);
      const total = Number(feedback.total || 0);
      feedbackBars[0].querySelector("i").style.width = `${total ? (positive / total) * 100 : 0}%`;
      feedbackBars[0].querySelector("strong").textContent = formatNumber(positive);
    }
    if (feedbackBars[1]) {
      const negative = Math.max(0, Number(feedback.total || 0) - Number(feedback.positive || 0));
      const total = Number(feedback.total || 0);
      feedbackBars[1].querySelector("i").style.width = `${total ? (negative / total) * 100 : 0}%`;
      feedbackBars[1].querySelector("strong").textContent = formatNumber(negative);
    }

    renderGovernanceEvents();
  }

  function renderGovernanceEvents() {
    const container = $("#governanceEventList");
    if (!container) return;
    const allEvents = currentEvents();
    const events = state.eventFilter
      ? allEvents.filter((event) => String(event.status || "").toUpperCase() === state.eventFilter)
      : allEvents;

    const head = `<div class="event-table-head"><span>事件</span><span>状态</span><span>时间</span></div>`;
    const rows = events
      .map((event) => {
        const meta = getEventMeta(event.event_type);
        const status = eventStatusMeta(event.status);
        return `<div class="event-table-row">
          <span><i class="event-dot ${meta.className}"></i><strong>${escapeHtml(meta.label)}</strong><small>${escapeHtml(eventDetail(event))}</small></span>
          <span class="status-pill ${status.className}">${escapeHtml(status.label)}</span>
          <time>${escapeHtml(relativeTime(event.created_at))}</time>
        </div>`;
      })
      .join("");
    container.innerHTML =
      head +
      (rows ||
        `<div class="empty-state"><svg><use href="#i-activity"></use></svg><strong>暂无事件</strong><span>当前筛选条件下没有事件记录。</span></div>`);
  }

  function maskToken(token) {
    if (!token) return "未配置";
    const tail = token.slice(-4);
    return `已配置 ···${tail}`;
  }

  function updateConnectionUi() {
    const pill = $(".connection-pill");
    const label = $("#connectionLabel");
    const connected = state.adminConnected || state.appConnected;
    pill.classList.toggle("is-connected", connected);
    if (state.adminConnected) label.textContent = "已连接";
    else if (state.appConnected) label.textContent = "应用已连接";
    else label.textContent = state.demoEnabled ? "示例数据" : "未连接";

    const status = $("#connectionStatus");
    if (status) {
      status.textContent = state.adminConnected
        ? "已连接"
        : state.appConnected
          ? "应用已连接"
          : state.demoEnabled
            ? "示例数据"
            : "未连接";
      status.className = `status-pill ${state.adminConnected || state.appConnected ? "is-ok" : "is-warn"}`;
    }
    $("#adminTokenState").textContent = maskToken(state.adminToken);
    $("#appTokenState").textContent = maskToken(state.appToken);
    $("#settingsBaseUrl").textContent = window.location.origin;
  }

  function updateManagementSelects() {
    fillSelect(
      $("#grantApp"),
      currentApps(),
      "id",
      (item) => `${item.name} · ${item.code}`,
      "请选择应用",
    );
    const tenants = currentTenants();
    fillSelect($("#kbTenant"), tenants, "id", (item) => `${item.name} · ${item.code}`);
    fillSelect($("#retentionTenant"), tenants, "id", (item) => `${item.name} · ${item.code}`);
    fillSelect(
      $("#documentKb"),
      currentKbs(),
      "id",
      (item) => `${item.name} · ${item.code}`,
      "请选择知识库",
    );
    fillSelect(
      $("#evalDataset"),
      currentDatasets(),
      "id",
      (item) => `${item.name} · ${item.code}`,
      "请选择数据集",
    );
  }

  function renderAll() {
    renderStats();
    renderCapabilityStrip();
    renderCapabilityTable();
    renderKbs();
    renderHomeEvents();
    renderGovernance();
    updateConnectionUi();
    updateManagementSelects();
  }

  function navigate(route, { scroll = true } = {}) {
    const next = routes[route] ? route : "overview";
    state.route = next;
    $$(".page").forEach((page) => {
      const active = page.dataset.page === next;
      page.hidden = !active;
      page.classList.toggle("is-active", active);
    });
    $$(".nav-item").forEach((item) => {
      const active = item.dataset.route === next;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $("#breadcrumbCurrent").textContent = routes[next].title;
    document.title = `${routes[next].title} · 司法 AI 中台`;
    closeSidebar();
    closeCommand();
    if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function routeFromHash() {
    const value = window.location.hash.replace(/^#\/?/, "");
    return routes[value] ? value : "overview";
  }

  function go(route) {
    if (window.location.hash === `#/${route}`) navigate(route);
    else window.location.hash = `#/${route}`;
  }

  function openSidebar() {
    $("#sidebar").classList.add("is-open");
    $(".sidebar-scrim").classList.add("is-visible");
  }

  function closeSidebar() {
    $("#sidebar").classList.remove("is-open");
    $(".sidebar-scrim").classList.remove("is-visible");
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("jai.theme", theme);
    } catch {
      /* Theme persistence is optional. */
    }
    const button = $("#themeToggle");
    button.setAttribute("aria-label", theme === "dark" ? "切换浅色模式" : "切换深色模式");
  }

  function toggleTheme() {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  }

  function toast(message) {
    const node = $("#toast");
    if (!node) return;
    window.clearTimeout(state.toastTimer);
    node.textContent = message;
    node.classList.add("is-visible");
    state.toastTimer = window.setTimeout(() => node.classList.remove("is-visible"), 3200);
  }

  function openTokenDialog() {
    const dialog = $("#tokenDialog");
    $("#adminTokenInput").value = state.adminToken;
    $("#appTokenInput").value = state.appToken;
    $("#tokenError").hidden = true;
    if (!dialog.open) dialog.showModal();
    window.setTimeout(() => $("#adminTokenInput").focus(), 40);
  }

  function closeTokenDialog() {
    const dialog = $("#tokenDialog");
    if (dialog.open) dialog.close();
  }

  function tokenError(message) {
    const node = $("#tokenError");
    node.textContent = message;
    node.hidden = false;
  }

  async function hydrateDashboard({ silent = false } = {}) {
    if (!state.adminToken) {
      renderAll();
      return;
    }
    const tasks = await Promise.allSettled([
      api("/api/v1/admin/capabilities"),
      api("/api/v1/kbs"),
      api("/api/v1/governance/metrics"),
      api("/api/v1/events?limit=40"),
      api("/api/v1/admin/apps"),
      api("/api/v1/admin/tenants"),
      api("/api/v1/datasets"),
    ]);
    const [capabilities, kbs, metrics, events, apps, tenants, datasets] = tasks;
    let successCount = 0;

    if (capabilities.status === "fulfilled") {
      state.capabilities = capabilities.value?.data?.items || [];
      successCount += 1;
    }
    if (kbs.status === "fulfilled") {
      state.kbs = kbs.value?.data?.items || [];
      successCount += 1;
    }
    if (metrics.status === "fulfilled") {
      state.metrics = metrics.value?.data || null;
      successCount += 1;
    }
    if (events.status === "fulfilled") {
      state.events = events.value?.data?.items || [];
      successCount += 1;
    }
    if (apps.status === "fulfilled") {
      state.apps = apps.value?.data?.items || [];
      successCount += 1;
    }
    if (tenants.status === "fulfilled") {
      state.tenants = tenants.value?.data?.items || [];
      successCount += 1;
    }
    if (datasets.status === "fulfilled") {
      state.datasets = datasets.value?.data?.items || [];
      successCount += 1;
    }

    if (!successCount) {
      const first = tasks.find((task) => task.status === "rejected");
      if (first && first.status === "rejected" && (first.reason?.status === 401 || first.reason?.status === 403)) {
        const error = new Error("管理令牌无效或权限不足");
        error.status = first.reason.status;
        throw error;
      }
      if (!silent) {
        toast(state.demoEnabled ? "未能读取平台数据，已保留示例视图" : "未能读取平台数据");
      }
    }
    state.adminConnected = successCount > 0;
    renderAll();
    if (!silent && successCount) toast("平台数据已更新");
  }

  async function connect(event) {
    event.preventDefault();
    const admin = $("#adminTokenInput").value.trim();
    const app = $("#appTokenInput").value.trim();
    const button = $("#connectBtn");
    if (!admin && !app) {
      tokenError("请至少填写一个令牌。");
      return;
    }
    $("#tokenError").hidden = true;
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "验证中…";
    try {
      let adminValid = false;
      let appValid = false;
      if (admin) {
        await api("/api/v1/admin/capabilities", { token: admin });
        adminValid = true;
      }
      if (app) {
        await api("/api/v1/kbs", { token: app });
        appValid = true;
      }

      state.adminToken = adminValid ? admin : "";
      state.appToken = appValid ? app : "";
      state.adminConnected = adminValid;
      state.appConnected = appValid;
      safeStorage.set("jai.adminToken", state.adminToken);
      safeStorage.set("jai.appToken", state.appToken);

      if (adminValid) {
        await hydrateDashboard({ silent: true });
      } else {
        state.capabilities = null;
        state.kbs = null;
        state.metrics = null;
        state.events = null;
        state.apps = null;
        state.tenants = null;
        state.datasets = null;
        state.datasetVersions.clear();
        renderAll();
      }
      closeTokenDialog();
      toast(adminValid ? "已连接平台，正在使用实时数据。" : "应用令牌已连接，检索实验台可用。");
    } catch (error) {
      tokenError(error.message || "连接失败，请检查令牌与服务状态。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function disconnect() {
    state.adminToken = "";
    state.appToken = "";
    state.adminConnected = false;
    state.appConnected = false;
    state.capabilities = null;
    state.kbs = null;
    state.metrics = null;
    state.events = null;
    state.apps = null;
    state.tenants = null;
    state.datasets = null;
    state.datasetVersions.clear();
    safeStorage.set("jai.adminToken", "");
    safeStorage.set("jai.appToken", "");
    $("#adminTokenInput").value = "";
    $("#appTokenInput").value = "";
    renderAll();
    toast(state.demoEnabled ? "已断开连接，当前为示例数据。" : "已断开连接。");
  }

  function restoreSession() {
    const admin = state.adminToken;
    const app = state.appToken;
    const jobs = [];
    if (admin) {
      jobs.push(
        hydrateDashboard({ silent: true }).catch(() => {
          state.adminToken = "";
          state.adminConnected = false;
          safeStorage.set("jai.adminToken", "");
        }),
      );
    }
    if (app) {
      jobs.push(
        api("/api/v1/kbs", { token: app })
          .then(() => {
            state.appConnected = true;
          })
          .catch(() => {
            state.appToken = "";
            state.appConnected = false;
            safeStorage.set("jai.appToken", "");
          }),
      );
    }
    Promise.allSettled(jobs).then(() => {
      renderAll();
    });
  }

  async function refreshCapabilities() {
    if (!state.adminToken) {
      renderAll();
      toast(state.demoEnabled ? "当前为示例数据。连接管理令牌后可读取实时能力。" : "连接管理令牌后可读取能力数据。");
      return;
    }
    try {
      await hydrateDashboard({ silent: true });
      toast("能力数据已刷新。");
    } catch (error) {
      toast(error.message || "刷新失败。");
    }
  }

  async function refreshGovernance() {
    if (!state.adminToken) {
      renderAll();
      toast(state.demoEnabled ? "当前为示例数据。连接管理令牌后可读取实时治理数据。" : "连接管理令牌后可读取治理数据。");
      return;
    }
    try {
      await hydrateDashboard({ silent: true });
      toast("治理态势已刷新。");
    } catch (error) {
      toast(error.message || "刷新失败。");
    }
  }

  async function processEvents() {
    if (!state.adminToken) {
      toast("连接管理令牌后才能处理待办事件。");
      return;
    }
    const button = $("#processEvents");
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = "处理中…";
    try {
      const result = await api("/api/v1/events/process", { method: "POST" });
      const processed = result?.data?.processed ?? 0;
      toast(`已处理 ${processed} 条待办事件。`);
      await hydrateDashboard({ silent: true });
    } catch (error) {
      toast(error.message || "处理事件失败。");
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function openCapabilityDetail(code) {
    const item = currentCapabilities().find((entry) => entry.code === code);
    if (!item) return;
    state.activeCapabilityCode = code;
    const meta = typeMeta[normalizeCapabilityType(item.type)] || typeMeta.GOVERNANCE;
    const status = statusMeta(item.status);
    $("#capabilityDialogIcon").innerHTML = `<svg><use href="#${meta.icon}"></use></svg>`;
    $("#capabilityDialogType").textContent = `${typeLabel(item.type).toUpperCase()} · CAPABILITY`;
    $("#capabilityDialogTitle").textContent = item.name || "能力详情";
    const versions = Array.isArray(item.versions) ? item.versions : [];
    const tags = Array.isArray(item.tags) ? item.tags : [];
    $("#capabilityDialogBody").innerHTML = `
      <p class="detail-description">${escapeHtml(item.description || "暂无描述。")}</p>
      <div class="detail-grid">
        <div><span>能力编码</span><strong>${escapeHtml(item.code)}</strong></div>
        <div><span>状态</span><strong>${escapeHtml(status.label)}</strong></div>
        <div><span>负责人</span><strong>${escapeHtml(item.owner || "—")}</strong></div>
        <div><span>默认版本</span><strong>${escapeHtml(item.default_version || versions[0]?.version || "—")}</strong></div>
      </div>
      ${tags.length ? `<div class="detail-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
      ${versions.length ? `<div class="detail-versions"><span>VERSIONS</span>${versions.slice(0, 4).map((version) => `<strong>${escapeHtml(version.version || version)} · ${escapeHtml(version.status || "")}</strong>`).join("<br>")}</div>` : ""}
    `;
    const defaultVersion = item.default_version || versions[0]?.version || "";
    $("#capabilityDialogActions").innerHTML = `
      ${defaultVersion ? `<button class="btn btn-quiet" type="button" data-publish-capability="${escapeHtml(item.code)}" data-version="${escapeHtml(defaultVersion)}"><svg><use href="#i-check"></use></svg>发布 ${escapeHtml(defaultVersion)}</button>` : ""}
      <button class="btn btn-quiet" type="button" data-grant-capability="${escapeHtml(item.code)}"><svg><use href="#i-lock"></use></svg>授权应用</button>
    `;
    const dialog = $("#capabilityDialog");
    if (!dialog.open) dialog.showModal();
  }

  function closeCapabilityDetail() {
    const dialog = $("#capabilityDialog");
    if (dialog.open) dialog.close();
  }

  function selectedKbIds() {
    return [...state.selectedKbIds];
  }

  function renderRetrievalResults(payload) {
    const results = Array.isArray(payload?.results) ? payload.results : [];
    const compression = payload?.compression || {};
    const decision = payload?.decision || {};
    const usage = payload?.usage || {};
    const container = $("#retrievalResults");
    if (!container) return;

    const savings = compression.ratio ? Math.round((1 - Number(compression.ratio)) * 100) : 0;
    $("#retrievalMeta").innerHTML = [
      `<span>${results.length} 条证据</span>`,
      `<span>耗时 ${formatNumber(usage.total_ms || 0)}ms</span>`,
      compression.enabled ? `<span>压缩 ${savings}%</span>` : `<span>未压缩</span>`,
    ].join("<i></i>");

    if (!results.length) {
      container.innerHTML = `<div class="results-head"><div><span class="card-kicker">RESULTS</span><h2>证据预览</h2></div></div><div class="empty-state"><svg><use href="#i-search"></use></svg><strong>没有召回结果</strong><span>尝试调整问题、知识库或 Top K。</span></div>`;
      return;
    }

    const items = results
      .map((item, index) => {
        const citation = item.citation || {};
        const title = citation.title || item.metadata?.title || "未命名证据";
        const docNo = citation.doc_no || citation.section_path || "";
        const page = citation.page ? `第 ${citation.page} 页` : "";
        const score = Number(item.score || 0);
        const source = docNo || page || "知识库";
        return `<article class="result-item">
          <div class="result-index">${String(index + 1).padStart(2, "0")}</div>
          <div class="result-main">
            <div class="result-title"><strong>${escapeHtml(title)}</strong><span class="badge ${index === 0 ? "badge-gold" : "badge-soft"}">${index === 0 ? "高相关" : "证据"}</span></div>
            <p>${escapeHtml(truncate(item.text || citation.snippet || "", 280))}</p>
            <div class="result-foot"><span>${escapeHtml(source)}</span><i></i><span>相关度 ${score.toFixed(3)}</span></div>
          </div>
          <button class="icon-btn result-link" type="button" aria-label="复制引用" data-copy="${escapeHtml(`${title}${docNo ? ` ${docNo}` : ""}`)}"><svg><use href="#i-copy"></use></svg></button>
        </article>`;
      })
      .join("");

    const integrity = compression.citation_integrity !== false;
    container.innerHTML = `
      <div class="results-head"><div><span class="card-kicker">RESULTS</span><h2>证据预览</h2></div><div class="result-meta" id="retrievalMeta"></div></div>
      <div class="result-list">${items}</div>
      <div class="results-foot">
        <span><svg><use href="#${integrity ? "i-check" : "i-alert"}"></use></svg>${integrity ? "引用完整性校验通过" : "引用校验未通过，已回退原文"}</span>
        <span>决策意图：${escapeHtml(intentLabel(decision.intent))} · 置信度 ${Number(decision.confidence || 0).toFixed(2)}</span>
      </div>`;
    $("#retrievalMeta").innerHTML = [
      `<span>${results.length} 条证据</span>`,
      `<span>耗时 ${formatNumber(usage.total_ms || 0)}ms</span>`,
      compression.enabled ? `<span>压缩 ${savings}%</span>` : `<span>未压缩</span>`,
    ].join("<i></i>");
  }

  async function runRetrieval(event) {
    event.preventDefault();
    const query = $("#retrievalQuery").value.trim();
    const kbIds = selectedKbIds();
    const topK = Number($("#retrievalTopK").value || 8);
    const compress = $("#retrievalCompress").checked;
    const button = $("#runRetrieval");
    if (!query) {
      toast("请输入检索问题。");
      $("#retrievalQuery").focus();
      return;
    }
    if (!kbIds.length) {
      toast("请至少选择一个知识库。");
      return;
    }

    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = "检索中…";
    try {
      if (!state.appToken) {
        await sleep(420);
        toast("当前为预览结果。连接应用令牌后可调用真实检索接口。");
        $("#retrievalMeta").innerHTML = `<span>3 条证据</span><i></i><span>耗时 312ms</span><i></i><span>压缩 41%</span>`;
        return;
      }
      const payload = await api("/v1/retrieve", {
        token: state.appToken,
        method: "POST",
        body: {
          kb_ids: kbIds,
          query,
          top_k: topK,
          with_citation: true,
          compress,
        },
      });
      renderRetrievalResults(payload);
      toast("检索完成，引用与压缩信息已更新。");
    } catch (error) {
      toast(error.message || "检索失败，请检查应用令牌与知识库状态。");
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  function commandList() {
    return [
      { group: "页面", label: "总览", hint: "Dashboard", icon: "i-home", run: () => go("overview") },
      { group: "页面", label: "能力网关", hint: "Capabilities", icon: "i-grid", run: () => go("capabilities") },
      { group: "页面", label: "知识检索", hint: "Retrieval lab", icon: "i-search", run: () => go("retrieval") },
      { group: "页面", label: "数据治理", hint: "Governance", icon: "i-shield", run: () => go("governance") },
      { group: "页面", label: "系统设置", hint: "Settings", icon: "i-settings", run: () => go("settings") },
      { group: "操作", label: "连接平台", hint: "配置管理令牌或应用令牌", icon: "i-lock", run: openTokenDialog },
      { group: "操作", label: "切换主题", hint: "浅色 / 深色", icon: "i-sun", run: toggleTheme },
      { group: "资源", label: "API 文档", hint: "OpenAPI / Swagger", icon: "i-link", run: () => window.open("/docs", "_blank", "noopener") },
      { group: "资源", label: "健康检查", hint: "/healthz", icon: "i-activity", run: () => window.open("/healthz", "_blank", "noopener") },
    ];
  }

  function renderCommandResults() {
    const input = $("#commandInput");
    const container = $("#commandResults");
    const query = input.value.trim().toLowerCase();
    const all = commandList();
    state.commandResults = all.filter((item) => `${item.label} ${item.hint} ${item.group}`.toLowerCase().includes(query));
    state.commandIndex = clamp(state.commandIndex, 0, Math.max(0, state.commandResults.length - 1));

    let lastGroup = "";
    container.innerHTML = state.commandResults.length
      ? state.commandResults
          .map((item, index) => {
            const group = item.group !== lastGroup ? `<div class="command-group">${escapeHtml(item.group)}</div>` : "";
            lastGroup = item.group;
            return `${group}<button class="command-item ${index === state.commandIndex ? "is-selected" : ""}" type="button" data-command-index="${index}">
              <svg><use href="#${item.icon}"></use></svg><span>${escapeHtml(item.label)}</span><small>${escapeHtml(item.hint)}</small>
            </button>`;
          })
          .join("")
      : `<div class="command-empty">没有匹配的页面或操作。</div>`;
  }

  function openCommand() {
    const overlay = $("#commandPalette");
    overlay.hidden = false;
    $("#commandInput").value = "";
    state.commandIndex = 0;
    renderCommandResults();
    window.setTimeout(() => $("#commandInput").focus(), 30);
  }

  function closeCommand() {
    const overlay = $("#commandPalette");
    if (overlay) overlay.hidden = true;
  }

  function executeCommand(index) {
    const item = state.commandResults[index];
    if (!item) return;
    closeCommand();
    item.run();
  }

  function copyText(text) {
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(
        () => toast("已复制到剪贴板。"),
        () => fallbackCopy(text),
      );
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("已复制到剪贴板。");
    } catch {
      toast("复制失败，请手动选择文本。");
    }
    area.remove();
  }

  function managementToken() {
    return state.adminToken || state.appToken;
  }

  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (dialog && !dialog.open) dialog.showModal();
  }

  function closeDialog(id) {
    const dialog = document.getElementById(id);
    if (dialog?.open) dialog.close();
  }

  function setFormError(id, message) {
    const node = document.getElementById(id);
    if (!node) return;
    node.textContent = message;
    node.hidden = !message;
  }

  async function apiForm(path, formData, token) {
    const response = await fetch(path, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
      credentials: "same-origin",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(
        payload?.error?.message || payload?.detail || `请求失败（HTTP ${response.status}）`,
      );
    }
    return payload;
  }

  function openCapabilityCreate() {
    $("#capabilityCreateForm").reset();
    $("#capOwner").value = "ai-platform";
    $("#capVersion").value = "1.0.0";
    $("#capProtocol").value = "PLATFORM";
    $("#capQps").value = "20";
    $("#capPublish").checked = true;
    setFormError("capCreateError", "");
    openDialog("capabilityCreateDialog");
  }

  async function submitCapability(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台并配置管理令牌。");
      return;
    }
    const button = $("#capCreateSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "创建中…";
    setFormError("capCreateError", "");
    const code = $("#capCode").value.trim();
    const version = $("#capVersion").value.trim();
    try {
      await api("/api/v1/admin/capabilities", {
        token,
        method: "POST",
        body: {
          code,
          name: $("#capName").value.trim(),
          type: $("#capType").value,
          scope: "GLOBAL",
          owner: $("#capOwner").value.trim(),
          version,
          protocol: $("#capProtocol").value,
          model_name: $("#capModelName").value.trim() || null,
          description: $("#capDescription").value.trim() || null,
          tags: $("#capTags")
            .value.split(",")
            .map((item) => item.trim())
            .filter(Boolean),
          qps_limit: Number($("#capQps").value) || null,
          request_schema: { type: "object" },
          response_schema: { type: "object" },
        },
      });
      if ($("#capPublish").checked) {
        await api(
          `/api/v1/admin/capabilities/${encodeURIComponent(code)}/versions/${encodeURIComponent(version)}/publish`,
          { token, method: "POST" },
        );
      }
      closeDialog("capabilityCreateDialog");
      await hydrateDashboard({ silent: true });
      toast($("#capPublish").checked ? "能力已创建并发布。" : "能力已创建为草稿。");
    } catch (error) {
      setFormError("capCreateError", error.message || "创建能力失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function publishCapability(code, version) {
    if (!state.adminToken) {
      toast("发布操作需要管理令牌。");
      return;
    }
    try {
      await api(
        `/api/v1/admin/capabilities/${encodeURIComponent(code)}/versions/${encodeURIComponent(version)}/publish`,
        { method: "POST" },
      );
      toast(`版本 ${version} 已发布。`);
      await hydrateDashboard({ silent: true });
      closeCapabilityDetail();
    } catch (error) {
      toast(error.message || "发布失败。");
    }
  }

  async function openGrantDialog(code) {
    state.activeCapabilityCode = code;
    $("#grantForm").reset();
    $("#grantEnv").value = "prod";
    setFormError("grantError", "");
    $("#grantContext").textContent = `能力 ${code} 的授权范围。授权后应用才能调用。`;
    updateManagementSelects();
    openDialog("grantDialog");
    await loadGrants(code);
  }

  async function loadGrants(code) {
    const container = $("#grantList");
    if (!container) return;
    if (!state.adminToken) {
      container.innerHTML = `<div class="dialog-copy">连接管理令牌后可查看已有授权。</div>`;
      return;
    }
    try {
      const payload = await api(
        `/api/v1/admin/capabilities/${encodeURIComponent(code)}/grants`,
      );
      const items = payload?.data?.items || [];
      container.innerHTML = items.length
        ? items
            .map(
              (grant) => `<div class="grant-row">
                <div><strong>${escapeHtml(grant.app_name || grant.app_code || `应用 ${grant.app_id}`)}</strong><span>${escapeHtml(grant.env || "prod")} · ${escapeHtml(grant.version || "全部版本")} · ${escapeHtml(statusMeta(grant.status).label)}</span></div>
                <span class="status-pill ${statusMeta(grant.status).className}">${escapeHtml(statusMeta(grant.status).label)}</span>
                <button type="button" data-revoke-grant="${escapeHtml(grant.id)}">撤销</button>
              </div>`,
            )
            .join("")
        : `<div class="dialog-copy">暂无授权记录。</div>`;
    } catch (error) {
      container.innerHTML = `<div class="dialog-copy">${escapeHtml(error.message || "授权列表读取失败")}</div>`;
    }
  }

  async function submitGrant(event) {
    event.preventDefault();
    const token = state.adminToken;
    const code = state.activeCapabilityCode;
    if (!token || !code) {
      toast("授权操作需要管理令牌。");
      return;
    }
    const appId = Number($("#grantApp").value);
    if (!appId) {
      setFormError("grantError", "请选择要授权的应用。");
      return;
    }
    const button = $("#grantSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "授权中…";
    setFormError("grantError", "");
    try {
      const kbIds = $("#grantKbIds")
        .value.split(",")
        .map((item) => Number(item.trim()))
        .filter((item) => Number.isFinite(item) && item > 0);
      const qps = Number($("#grantQps").value);
      await api(`/api/v1/admin/capabilities/${encodeURIComponent(code)}/grants`, {
        token,
        method: "POST",
        body: {
          app_id: appId,
          version: $("#grantVersion").value.trim() || null,
          env: $("#grantEnv").value,
          quota: qps ? { qps } : null,
          data_scope: kbIds.length ? { kb_ids: kbIds } : null,
        },
      });
      toast("授权已生效。");
      await loadGrants(code);
      await hydrateDashboard({ silent: true });
    } catch (error) {
      setFormError("grantError", error.message || "授权失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function revokeGrant(grantId) {
    const code = state.activeCapabilityCode;
    if (!state.adminToken || !code) return;
    try {
      await api(
        `/api/v1/admin/capabilities/${encodeURIComponent(code)}/grants/${encodeURIComponent(grantId)}`,
        { method: "DELETE" },
      );
      toast("授权已撤销。");
      await loadGrants(code);
      await hydrateDashboard({ silent: true });
    } catch (error) {
      toast(error.message || "撤销授权失败。");
    }
  }

  function openKbCreate() {
    $("#kbCreateForm").reset();
    $("#kbOwner").value = "ai-platform";
    setFormError("kbCreateError", "");
    updateManagementSelects();
    const tenants = currentTenants();
    if (tenants.length) $("#kbTenant").value = String(tenants[0].id);
    openDialog("kbCreateDialog");
  }

  async function submitKb(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台。");
      return;
    }
    const tenantId = Number($("#kbTenant").value);
    if (!tenantId) {
      setFormError("kbCreateError", "请选择所属租户。");
      return;
    }
    const button = $("#kbCreateSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "创建中…";
    setFormError("kbCreateError", "");
    try {
      await api("/api/v1/kbs", {
        token,
        method: "POST",
        body: {
          code: $("#kbCode").value.trim(),
          name: $("#kbName").value.trim(),
          tenant_id: tenantId,
          owner: $("#kbOwner").value.trim(),
          domain: $("#kbDomain").value.trim() || null,
          description: $("#kbDescription").value.trim() || null,
          security_level: $("#kbSecurity").value,
        },
      });
      closeDialog("kbCreateDialog");
      await hydrateDashboard({ silent: true });
      toast("知识库已创建，可继续上传文档。");
    } catch (error) {
      setFormError("kbCreateError", error.message || "创建知识库失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function openDocumentDialog(kbId = null) {
    $("#documentForm").reset();
    setFormError("documentError", "");
    $("#documentIndex").checked = true;
    fillSelect(
      $("#documentKb"),
      currentKbs(),
      "id",
      (item) => `${item.name} · ${item.code}`,
      "请选择知识库",
    );
    if (kbId) $("#documentKb").value = String(kbId);
    openDialog("documentDialog");
  }

  async function submitDocument(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台。");
      return;
    }
    const kbId = Number($("#documentKb").value);
    if (!kbId) {
      setFormError("documentError", "请选择目标知识库。");
      return;
    }
    const file = $("#documentFile").files[0];
    const text = $("#documentText").value.trim();
    if (!file && !text) {
      setFormError("documentError", "请选择文件或粘贴文本。");
      return;
    }
    const button = $("#documentSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "入库中…";
    setFormError("documentError", "");
    try {
      let result;
      if (file) {
        const form = new FormData();
        form.append("file", file);
        form.append("title", $("#documentTitleInput").value.trim() || file.name);
        form.append("doc_type", "file");
        form.append("security_level", $("#documentSecurity").value);
        result = await apiForm(`/api/v1/kbs/${kbId}/documents/upload`, form, token);
      } else {
        result = await api(`/api/v1/kbs/${kbId}/documents`, {
          token,
          method: "POST",
          body: {
            title: $("#documentTitleInput").value.trim() || "未命名文档",
            text,
            doc_type: "text",
            security_level: $("#documentSecurity").value,
          },
        });
      }
      if ($("#documentIndex").checked) {
        const job = await api(`/api/v1/kbs/${kbId}/index-jobs`, {
          token,
          method: "POST",
        });
        const status = job?.data?.status;
        toast(
          status === "PENDING"
            ? "文档已入库，索引任务已排队。"
            : "文档已入库并完成索引。",
        );
      } else {
        toast("文档已入库，尚未构建索引。");
      }
      closeDialog("documentDialog");
      await hydrateDashboard({ silent: true });
    } catch (error) {
      setFormError("documentError", error.message || "文档入库失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function loadEvalVersions() {
    const datasetId = Number($("#evalDataset").value);
    const select = $("#evalDatasetVersion");
    if (!datasetId) {
      select.innerHTML = `<option value="">请选择数据集</option>`;
      return;
    }
    if (state.datasetVersions.has(datasetId)) {
      fillSelect(
        select,
        state.datasetVersions.get(datasetId),
        "id",
        (item) => `${item.version} · ${item.status}`,
      );
      return;
    }
    try {
      const payload = await api(`/api/v1/datasets/${datasetId}/versions`);
      const items = payload?.data?.items || [];
      state.datasetVersions.set(datasetId, items);
      fillSelect(select, items, "id", (item) => `${item.version} · ${item.status}`);
    } catch (error) {
      select.innerHTML = `<option value="">${escapeHtml(error.message || "版本读取失败")}</option>`;
    }
  }

  async function openEvalDialog() {
    $("#evalForm").reset();
    setFormError("evalError", "");
    $("#evalTopK").value = "5";
    $("#evalThreshold").value = "0.8";
    updateManagementSelects();
    openDialog("evalDialog");
    if (currentDatasets().length) {
      $("#evalDataset").value = String(currentDatasets()[0].id);
      await loadEvalVersions();
    } else {
      $("#evalDatasetVersion").innerHTML = `<option value="">暂无数据集</option>`;
    }
  }

  async function submitEval(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台。");
      return;
    }
    const button = $("#evalSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "创建中…";
    setFormError("evalError", "");
    try {
      const payload = await api("/api/v1/evals", {
        token,
        method: "POST",
        body: {
          dataset_version_id: Number($("#evalDatasetVersion").value),
          target_type: $("#evalTargetType").value,
          target_version: $("#evalTargetVersion").value.trim(),
          config: {
            top_k: Number($("#evalTopK").value) || 5,
            pass_threshold: Number($("#evalThreshold").value) || 0.8,
          },
        },
      });
      closeDialog("evalDialog");
      await hydrateDashboard({ silent: true });
      toast(payload?.data?.job ? "评测已提交异步执行。" : "评测已完成。");
    } catch (error) {
      setFormError("evalError", error.message || "创建评测失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function openAnnotationDialog() {
    $("#annotationForm").reset();
    $("#annotationTotal").value = "20";
    $("#annotationReviewers").value = "1";
    setFormError("annotationError", "");
    openDialog("annotationDialog");
  }

  async function submitAnnotation(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台。");
      return;
    }
    const due = $("#annotationDue").value;
    const button = $("#annotationSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "创建中…";
    setFormError("annotationError", "");
    try {
      await api("/api/v1/annotation-tasks", {
        token,
        method: "POST",
        body: {
          name: $("#annotationName").value.trim(),
          task_type: $("#annotationType").value,
          total_items: Number($("#annotationTotal").value) || 0,
          required_reviewers: Number($("#annotationReviewers").value) || 1,
          assignee: $("#annotationAssignee").value.trim() || null,
          due_at: due ? new Date(due).toISOString() : null,
        },
      });
      closeDialog("annotationDialog");
      await hydrateDashboard({ silent: true });
      toast("标注任务已创建。");
    } catch (error) {
      setFormError("annotationError", error.message || "创建标注任务失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function openRetentionDialog() {
    $("#retentionForm").reset();
    $("#retentionDryRun").checked = true;
    setFormError("retentionError", "");
    updateManagementSelects();
    const tenants = currentTenants();
    if (tenants.length) $("#retentionTenant").value = String(tenants[0].id);
    openDialog("retentionDialog");
  }

  async function submitRetention(event) {
    event.preventDefault();
    const token = managementToken();
    if (!token) {
      toast("请先连接平台。");
      return;
    }
    const before = $("#retentionBefore").value;
    const button = $("#retentionSubmit");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "执行中…";
    setFormError("retentionError", "");
    try {
      const payload = await api("/api/v1/retention-jobs", {
        token,
        method: "POST",
        body: {
          target: $("#retentionTarget").value,
          before: before ? new Date(before).toISOString() : null,
          tenant_id: Number($("#retentionTenant").value),
          dry_run: $("#retentionDryRun").checked,
        },
      });
      closeDialog("retentionDialog");
      await hydrateDashboard({ silent: true });
      const deleted = payload?.data?.deleted_count ?? 0;
      toast(
        $("#retentionDryRun").checked
          ? `试运行完成，将影响 ${formatNumber(deleted)} 条记录。`
          : `保留任务完成，已删除 ${formatNumber(deleted)} 条记录。`,
      );
    } catch (error) {
      setFormError("retentionError", error.message || "保留任务执行失败。");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function bindEvents() {
    document.addEventListener("click", (event) => {
      const routeButton = event.target.closest("[data-route]");
      if (routeButton) {
        event.preventDefault();
        go(routeButton.dataset.route);
        return;
      }

      if (event.target.closest("[data-open-sidebar]")) {
        openSidebar();
        return;
      }
      if (event.target.closest("[data-close-sidebar]")) {
        closeSidebar();
        return;
      }
      if (event.target.closest("[data-open-token]")) {
        openTokenDialog();
        return;
      }
      if (event.target.closest("[data-close-token]")) {
        closeTokenDialog();
        return;
      }
      if (event.target.closest("[data-open-command]")) {
        openCommand();
        return;
      }
      if (event.target.closest("[data-close-command]")) {
        closeCommand();
        return;
      }
      const closeDialogButton = event.target.closest("[data-close-dialog]");
      if (closeDialogButton) {
        closeDialog(closeDialogButton.dataset.closeDialog);
        return;
      }
      if (event.target.closest("[data-open-capability-create]")) {
        openCapabilityCreate();
        return;
      }
      if (event.target.closest("[data-open-kb-create]")) {
        openKbCreate();
        return;
      }
      if (event.target.closest("[data-open-document]")) {
        openDocumentDialog();
        return;
      }
      if (event.target.closest("[data-open-eval]")) {
        openEvalDialog();
        return;
      }
      if (event.target.closest("[data-open-annotation]")) {
        openAnnotationDialog();
        return;
      }
      if (event.target.closest("[data-open-retention]")) {
        openRetentionDialog();
        return;
      }
      const publishButton = event.target.closest("[data-publish-capability]");
      if (publishButton) {
        publishCapability(
          publishButton.dataset.publishCapability,
          publishButton.dataset.version,
        );
        return;
      }
      const grantButton = event.target.closest("[data-grant-capability]");
      if (grantButton) {
        closeCapabilityDetail();
        openGrantDialog(grantButton.dataset.grantCapability);
        return;
      }
      const revokeButton = event.target.closest("[data-revoke-grant]");
      if (revokeButton) {
        revokeGrant(revokeButton.dataset.revokeGrant);
        return;
      }
      if (event.target.closest("[data-close-capability]")) {
        closeCapabilityDetail();
        return;
      }

      const capabilityButton = event.target.closest("[data-open-capability]");
      if (capabilityButton) {
        openCapabilityDetail(capabilityButton.dataset.openCapability);
        return;
      }

      const capabilityRow = event.target.closest("[data-capability-code]");
      if (capabilityRow) {
        openCapabilityDetail(capabilityRow.dataset.capabilityCode);
        return;
      }

      const copyButton = event.target.closest("[data-copy]");
      if (copyButton) {
        copyText(copyButton.dataset.copy || "");
        return;
      }

      const commandItem = event.target.closest("[data-command-index]");
      if (commandItem) {
        executeCommand(Number(commandItem.dataset.commandIndex));
        return;
      }

      const eventFilter = event.target.closest("[data-event-filter]");
      if (eventFilter) {
        state.eventFilter = eventFilter.dataset.eventFilter;
        $$("[data-event-filter]").forEach((button) => button.classList.toggle("is-active", button === eventFilter));
        renderGovernanceEvents();
        return;
      }

      const chip = event.target.closest("[data-kb-id]");
      if (chip) {
        const id = Number(chip.dataset.kbId);
        if (state.selectedKbIds.has(id)) state.selectedKbIds.delete(id);
        else state.selectedKbIds.add(id);
        chip.classList.toggle("is-selected", state.selectedKbIds.has(id));
      }
    });

    document.addEventListener("keydown", (event) => {
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        if ($("#commandPalette").hidden) openCommand();
        else closeCommand();
        return;
      }
      if (event.key === "Escape") {
        closeSidebar();
        closeCommand();
        return;
      }
      if ($("#commandPalette").hidden) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        state.commandIndex = Math.min(state.commandIndex + 1, state.commandResults.length - 1);
        renderCommandResults();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        state.commandIndex = Math.max(state.commandIndex - 1, 0);
        renderCommandResults();
      } else if (event.key === "Enter") {
        event.preventDefault();
        executeCommand(state.commandIndex);
      }
    });

    $("#commandInput").addEventListener("input", () => {
      state.commandIndex = 0;
      renderCommandResults();
    });

    $("#themeToggle").addEventListener("click", toggleTheme);
    $("#tokenForm").addEventListener("submit", connect);
    $("#disconnectBtn").addEventListener("click", disconnect);
    $("#refreshCapabilities").addEventListener("click", refreshCapabilities);
    $("#refreshGovernance").addEventListener("click", refreshGovernance);
    $("#processEvents").addEventListener("click", processEvents);
    $("#retrievalForm").addEventListener("submit", runRetrieval);
    $("#capabilitySearch").addEventListener("input", renderCapabilityTable);
    $("#capabilityType").addEventListener("change", renderCapabilityTable);
    $("#capabilityCreateForm").addEventListener("submit", submitCapability);
    $("#grantForm").addEventListener("submit", submitGrant);
    $("#kbCreateForm").addEventListener("submit", submitKb);
    $("#documentForm").addEventListener("submit", submitDocument);
    $("#evalForm").addEventListener("submit", submitEval);
    $("#annotationForm").addEventListener("submit", submitAnnotation);
    $("#retentionForm").addEventListener("submit", submitRetention);
    $("#evalDataset").addEventListener("change", loadEvalVersions);

    $("#retrievalQuery").addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        $("#retrievalForm").requestSubmit();
      }
    });

    [
      "tokenDialog",
      "capabilityDialog",
      "capabilityCreateDialog",
      "grantDialog",
      "kbCreateDialog",
      "documentDialog",
      "evalDialog",
      "annotationDialog",
      "retentionDialog",
    ].forEach((id) => {
      const dialog = document.getElementById(id);
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });

    window.addEventListener("hashchange", () => navigate(routeFromHash()));
  }

  function startClock() {
    const now = new Date();
    $("#todayLabel").textContent = now.toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
    const tick = () => {
      $("#clockLabel").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    };
    tick();
    window.setInterval(tick, 1000);
  }

  async function loadRuntimeMode() {
    try {
      const response = await fetch("/version", { headers: { Accept: "application/json" } });
      const payload = response.ok ? await response.json() : {};
      state.environment = String(payload.environment || "unknown").toLowerCase();
      state.demoEnabled = !["prod", "production"].includes(state.environment);
    } catch {
      state.environment = "unknown";
      state.demoEnabled = false;
    }
  }

  async function init() {
    let storedTheme = "";
    try {
      storedTheme = localStorage.getItem("jai.theme") || "";
    } catch {
      storedTheme = "";
    }
    const preferred = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    setTheme(storedTheme || preferred);

    await loadRuntimeMode();
    renderSparks();
    renderAll();
    startClock();
    bindEvents();
    navigate(routeFromHash(), { scroll: false });
    restoreSession();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();


