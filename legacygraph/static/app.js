(() => {
  "use strict";

  const tokenMeta = document.querySelector('meta[name="legacygraph-token"]');
  const SESSION_TOKEN = tokenMeta ? tokenMeta.getAttribute("content") || "" : "";

  const state = {
    epoch: 0,
    contextKey: "",
    contexts: [],
    status: null,
    selectedKey: null,
    seeds: new Set(),
    seedGraphs: new Map(),   // seedKey -> { nodeIds: Set, edgeIds: Set }
    expansions: new Map(),   // expanderKey -> { nodeIds: Set, edgeIds: Set }
    pendingExpansions: new Map(),
    pathOverlay: { nodeIds: new Set(), edgeIds: new Set(), steps: [] },
    nodeCatalog: new Map(),  // key -> normalized node
    edgeCatalog: new Map(),  // edgeId -> normalized edge
    activeNodes: [],
    activeEdges: [],
    nodePositions: new Map(),
    truncated: false,
    partialIndex: false,
    panX: 40,
    panY: 40,
    scale: 1,
  };

  const el = (id) => document.getElementById(id);
  const nodeKey = (obj) => String((obj && (obj.semantic_key || obj.id)) || "");

  function normalizeNode(raw) {
    const k = nodeKey(raw);
    return {
      ...raw,
      id: String(raw.id || k),
      semantic_key: String(raw.semantic_key || k),
      name: String(raw.name || raw.qualified_name || k),
      qualified_name: String(raw.qualified_name || raw.name || k),
      kind: String(raw.kind || "external_symbol"),
      is_external: Boolean(raw.is_external || raw.kind === "external_symbol"),
      is_unresolved_placeholder: Boolean(raw.is_unresolved_placeholder),
    };
  }

  function normalizeEdge(raw, idx = 0) {
    const src = String(raw.source || raw.source_semantic_key || "");
    const tgt = String(raw.target || raw.target_semantic_key || "");
    const kind = String(raw.kind || "calls");
    const status = String(raw.resolution_status || "resolved");
    const dispatch = String(
      raw.dispatch_kind ||
        (kind === "handles_event" || kind === "registers_callback" ? "event-registration" : "direct")
    );
    const id = String(raw.id || raw.relation_id || `e:${idx}:${src}:${kind}:${tgt}`);
    return {
      ...raw,
      id,
      source: src,
      target: tgt,
      kind,
      resolution_status: status,
      dispatch_kind: dispatch,
      evidence: Array.isArray(raw.evidence) ? raw.evidence : [],
      candidates: Array.isArray(raw.candidates) ? raw.candidates : [],
    };
  }

  function showAlert(msg) {
    const banner = el("global-alert");
    if (!msg) {
      banner.textContent = "";
      banner.classList.add("hidden");
      return;
    }
    banner.textContent = String(msg);
    banner.classList.remove("hidden");
  }

  async function apiFetch(path, options = {}) {
    const epoch = state.epoch;
    const headers = Object.assign({ "X-LegacyGraph-Token": SESSION_TOKEN }, options.headers || {});
    const res = await fetch(path, { ...options, headers });
    let data = null;
    try {
      data = await res.json();
    } catch (_err) {
      throw new Error(`HTTP ${res.status}: non-JSON response`);
    }
    if (epoch !== state.epoch) {
      const error = new Error("Query scope changed.");
      error.name = "AbortError";
      throw error;
    }
    if (!res.ok || (data && data.ok === false)) {
      const errMsg = (data && (data.error || data.message)) || `Request failed (${res.status})`;
      throw new Error(typeof errMsg === "string" ? errMsg : JSON.stringify(errMsg));
    }
    return data;
  }

  function getControls() {
    const contextKey = el("context-select").value || state.contextKey;
    const direction = el("direction-select").value || "out";
    const depth = Math.max(1, Math.min(6, parseInt(el("depth-input").value, 10) || 1));
    const maxNodes = Math.max(10, Math.min(500, parseInt(el("max-nodes-input").value, 10) || 100));
    const maxEdges = Math.max(10, Math.min(1500, parseInt(el("max-edges-input").value, 10) || 300));
    const kinds = Array.from(document.querySelectorAll('input[name="rel-kind"]:checked')).map((i) => i.value);
    const statuses = Array.from(document.querySelectorAll('input[name="res-status"]:checked')).map((i) => i.value);
    return { contextKey, direction, depth, maxNodes, maxEdges, kinds, statuses };
  }

  function makeBadge(text, cls = "badge-neutral") {
    const span = document.createElement("span");
    span.className = `badge ${cls}`;
    span.textContent = String(text);
    return span;
  }

  async function initApp() {
    bindEvents();
    try {
      const [statusData, ctxData] = await Promise.all([
        apiFetch("/api/status"),
        apiFetch("/api/contexts"),
      ]);
      state.status = statusData;
      const pub = statusData.published_generation;
      const statusBadge = el("status-badge");
      if (pub && pub.id != null) {
        statusBadge.textContent = `Gen #${pub.id} · ${pub.verified_tu_runs || 0} TUs`;
      } else {
        statusBadge.textContent = `Files: ${statusData.active_files || 0}`;
      }
      state.partialIndex = Boolean(
        statusData.partial_index ||
          statusData.has_incomplete_dependencies ||
          (statusData.invalidated_translation_units && statusData.invalidated_translation_units.length > 0)
      );
      el("partial-badge").classList.toggle("hidden", !state.partialIndex);

      const registered = Array.isArray(ctxData.contexts) ? ctxData.contexts : [];
      const active = registered.filter((c) => Number(c.active_translation_units ?? 1) > 0);
      const contexts = active.length ? active : registered;
      state.contexts = contexts;
      const sel = el("context-select");
      sel.replaceChildren();
      contexts.forEach((c) => {
        const opt = document.createElement("option");
        const key = String(c.context_key || c.key || c.id || "");
        opt.value = key;
        const label = c.project_name
          ? `${c.project_name} [${c.configuration || ""}|${c.platform || ""}] · ${key.slice(-6)}`
          : key;
        opt.textContent = label;
        sel.appendChild(opt);
      });
      if (contexts.length > 0) {
        state.contextKey = sel.value;
      }
      await runSearch("");
    } catch (err) {
      showAlert(`Initialization error: ${err.message}`);
    }
  }

  async function runSearch(queryText) {
    const { contextKey } = getControls();
    const meta = el("search-meta");
    const list = el("search-results");
    meta.textContent = "Searching symbols…";
    try {
      const qs = new URLSearchParams({ query: queryText, limit: "50" });
      if (contextKey) qs.set("context_key", contextKey);
      const data = await apiFetch(`/api/search?${qs.toString()}`);
      const items = (data.items || data.symbols || []).map(normalizeNode);
      items.forEach((n) => state.nodeCatalog.set(nodeKey(n), n));
      meta.textContent = `${items.length} symbol(s)${data.truncated ? " (truncated)" : ""}`;
      list.replaceChildren();
      if (items.length === 0) {
        const empty = document.createElement("li");
        empty.className = "meta-line";
        empty.textContent = "No matching symbols in active build context.";
        list.appendChild(empty);
        return;
      }
      items.forEach((sym) => {
        const key = nodeKey(sym);
        const li = document.createElement("li");
        li.className = "result-item" + (state.selectedKey === key ? " active" : "");
        li.setAttribute("role", "option");
        li.setAttribute("tabindex", "0");

        const top = document.createElement("div");
        top.className = "section-header-row";
        const title = document.createElement("span");
        title.className = "item-title";
        title.textContent = sym.name;
        top.appendChild(title);
        top.appendChild(makeBadge(sym.kind, "badge-kind"));

        const sub = document.createElement("div");
        sub.className = "item-sub";
        sub.textContent = sym.qualified_name;

        li.append(top, sub);
        const activate = () => loadSeedSymbol(key);
        li.addEventListener("click", activate);
        li.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            activate();
          }
        });
        list.appendChild(li);
      });
    } catch (err) {
      if (err.name === "AbortError") return;
      meta.textContent = `Search failed: ${err.message}`;
    }
  }

  function registerGraphSlice(nodes, edges) {
    const nodeIds = new Set();
    const edgeIds = new Set();
    (nodes || []).forEach((raw) => {
      const n = normalizeNode(raw);
      const k = nodeKey(n);
      if (k) {
        state.nodeCatalog.set(k, n);
        nodeIds.add(k);
      }
    });
    (edges || []).forEach((raw, idx) => {
      const e = normalizeEdge(raw, idx);
      state.edgeCatalog.set(e.id, e);
      edgeIds.add(e.id);
      if (e.source) nodeIds.add(e.source);
      if (e.target) nodeIds.add(e.target);
    });
    return { nodeIds, edgeIds };
  }

  async function loadSeedSymbol(symKey) {
    showAlert("");
    const { contextKey, direction, depth, maxNodes, maxEdges, kinds, statuses } = getControls();
    state.seeds.add(symKey);
    try {
      const qs = new URLSearchParams({
        seeds: symKey,
        context_key: contextKey,
        direction,
        depth: String(depth),
        max_nodes: String(maxNodes),
        max_edges: String(maxEdges),
      });
      if (kinds.length) qs.set("kinds", kinds.join(","));
      if (statuses.length) qs.set("statuses", statuses.join(","));

      const data = await apiFetch(`/api/subgraph?${qs.toString()}`);
      const slice = registerGraphSlice(data.nodes, data.edges);
      slice.nodeIds.add(symKey);
      state.seedGraphs.set(symKey, slice);
      state.truncated = Boolean(data.truncated);
      if (data.incomplete || data.generation?.incomplete || data.partial_index || data.has_incomplete_dependencies) {
        state.partialIndex = true;
      }
      recomputeActiveGraph();
      fitGraphToView();
      await selectSymbol(symKey);
    } catch (err) {
      if (err.name === "AbortError") return;
      showAlert(`Failed to load subgraph: ${err.message}`);
    }
  }

  async function expandNode(symKey) {
    if (!symKey) return;
    const ticket = Symbol(symKey);
    state.pendingExpansions.set(symKey, ticket);
    showAlert("");
    const { contextKey, direction, maxEdges, kinds, statuses } = getControls();
    try {
      const qs = new URLSearchParams({
        semantic_key: symKey,
        context_key: contextKey,
        direction,
        limit: String(maxEdges),
      });
      if (kinds.length) qs.set("kinds", kinds.join(","));
      if (statuses.length) qs.set("statuses", statuses.join(","));
      const data = await apiFetch(`/api/neighbors?${qs.toString()}`);
      if (state.pendingExpansions.get(symKey) !== ticket) return;
      state.pendingExpansions.delete(symKey);
      const slice = registerGraphSlice(data.nodes, data.edges);
      slice.nodeIds.add(symKey);
      state.expansions.set(symKey, slice);
      if (data.truncated) state.truncated = true;
      recomputeActiveGraph();
      await selectSymbol(symKey);
    } catch (err) {
      if (err.name === "AbortError") return;
      showAlert(`Expand failed: ${err.message}`);
    }
  }

  function collapseNode(symKey) {
    if (!symKey) return;
    state.pendingExpansions.delete(symKey);
    state.expansions.delete(symKey);
    if (state.seedGraphs.has(symKey)) {
      // Preserve the root seed node itself while dropping its expanded seed neighborhood
      state.seedGraphs.set(symKey, { nodeIds: new Set([symKey]), edgeIds: new Set() });
    }
    recomputeActiveGraph();
    if (state.selectedKey === symKey) {
      renderSymbolDetails(state.nodeCatalog.get(symKey));
    }
  }

  function recomputeActiveGraph() {
    const { maxNodes, maxEdges } = getControls();
    const retainedNodeIds = new Set();
    const retainedEdgeIds = new Set();

    const mergeOwner = (owner) => {
      owner.nodeIds.forEach((id) => retainedNodeIds.add(id));
      owner.edgeIds.forEach((id) => retainedEdgeIds.add(id));
    };

    state.seedGraphs.forEach(mergeOwner);
    state.expansions.forEach(mergeOwner);
    if (state.pathOverlay.nodeIds.size > 0) {
      mergeOwner(state.pathOverlay);
    }

    let capTruncated = false;
    const nodes = [];
    for (const id of retainedNodeIds) {
      if (nodes.length >= maxNodes) {
        capTruncated = true;
        break;
      }
      const n = state.nodeCatalog.get(id);
      if (!n) continue;
      nodes.push(n);
    }
    const allowedNodeSet = new Set(nodes.map((n) => nodeKey(n)));

    const edges = [];
    for (const eid of retainedEdgeIds) {
      const e = state.edgeCatalog.get(eid);
      if (!e) continue;
      if (!allowedNodeSet.has(e.source) || (e.target && !allowedNodeSet.has(e.target))) continue;
      if (edges.length >= maxEdges) {
        capTruncated = true;
        break;
      }
      edges.push(e);
    }

    state.activeNodes = nodes;
    state.activeEdges = edges;
    if (capTruncated) state.truncated = true;

    computeLayeredLayout(nodes, edges);
    renderGraph();
    renderGraphBanners();
  }

  function computeLayeredLayout(nodes, edges) {
    state.nodePositions.clear();
    if (nodes.length === 0) return;

    const adj = new Map();
    const indeg = new Map();
    nodes.forEach((n) => {
      const k = nodeKey(n);
      adj.set(k, []);
      indeg.set(k, 0);
    });
    edges.forEach((e) => {
      if (adj.has(e.source) && adj.has(e.target) && e.source !== e.target) {
        adj.get(e.source).push(e.target);
        indeg.set(e.target, (indeg.get(e.target) || 0) + 1);
      }
    });

    const layerByKey = new Map();
    const queue = [];
    state.seeds.forEach((s) => {
      if (adj.has(s)) {
        layerByKey.set(s, 0);
        queue.push(s);
      }
    });
    nodes.forEach((n) => {
      const k = nodeKey(n);
      if (!layerByKey.has(k) && (indeg.get(k) || 0) === 0) {
        layerByKey.set(k, 0);
        queue.push(k);
      }
    });

    while (queue.length > 0) {
      const curr = queue.shift();
      const currLayer = layerByKey.get(curr) || 0;
      for (const nxt of adj.get(curr) || []) {
        if (!layerByKey.has(nxt)) {
          layerByKey.set(nxt, Math.min(6, currLayer + 1));
          queue.push(nxt);
        }
      }
    }

    const columns = new Map();
    nodes.forEach((n) => {
      const k = nodeKey(n);
      const col = layerByKey.has(k) ? layerByKey.get(k) : 1;
      if (!columns.has(col)) columns.set(col, []);
      columns.get(col).push(n);
    });

    const colWidth = 240;
    const rowHeight = 74;
    Array.from(columns.keys())
      .sort((a, b) => a - b)
      .forEach((colIdx) => {
        const colNodes = columns.get(colIdx);
        colNodes.forEach((n, rowIdx) => {
          state.nodePositions.set(nodeKey(n), {
            x: 40 + colIdx * colWidth,
            y: 40 + rowIdx * rowHeight,
            w: 185,
            h: 48,
          });
        });
      });
  }

  function classifyEdge(edge) {
    if (edge.resolution_status !== "resolved") {
      return { cls: "edge-ambiguous", marker: "url(#arrow-ambiguous)", tag: `[${edge.resolution_status}]` };
    }
    if (
      edge.dispatch_kind === "event-registration" ||
      edge.kind === "handles_event" ||
      edge.kind === "registers_callback"
    ) {
      return { cls: "edge-event", marker: "url(#arrow-event)", tag: "[event-reg]" };
    }
    if (
      edge.dispatch_kind === "virtual" ||
      edge.dispatch_kind === "indirect" ||
      edge.kind === "may_dispatch_to" ||
      edge.kind === "overrides"
    ) {
      return { cls: "edge-virtual", marker: "url(#arrow-virtual)", tag: `[${edge.dispatch_kind}]` };
    }
    return { cls: "edge-direct", marker: "url(#arrow-direct)", tag: "" };
  }

  function renderGraph() {
    const svgNS = "http://www.w3.org/2000/svg";
    const edgesGroup = el("graph-edges");
    const nodesGroup = el("graph-nodes");
    edgesGroup.replaceChildren();
    nodesGroup.replaceChildren();

    el("graph-empty").classList.toggle("hidden", state.activeNodes.length > 0);

    state.activeEdges.forEach((edge) => {
      const srcPos = state.nodePositions.get(edge.source);
      const tgtPos = state.nodePositions.get(edge.target);
      if (!srcPos || !tgtPos) return;

      const x1 = srcPos.x + srcPos.w;
      const y1 = srcPos.y + srcPos.h / 2;
      const x2 = tgtPos.x;
      const y2 = tgtPos.y + tgtPos.h / 2;
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.45);

      const styleInfo = classifyEdge(edge);
      const g = document.createElementNS(svgNS, "g");
      const isPath = state.pathOverlay.edgeIds.has(edge.id);
      g.setAttribute("class", `graph-edge ${styleInfo.cls}${isPath ? " edge-path" : ""}`);

      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`);
      path.setAttribute("marker-end", styleInfo.marker);
      g.appendChild(path);

      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", String((x1 + x2) / 2));
      label.setAttribute("y", String((y1 + y2) / 2 - 5));
      label.setAttribute("text-anchor", "middle");
      label.textContent = styleInfo.tag ? `${edge.kind} ${styleInfo.tag}` : edge.kind;
      g.appendChild(label);

      edgesGroup.appendChild(g);
    });

    state.activeNodes.forEach((node) => {
      const k = nodeKey(node);
      const pos = state.nodePositions.get(k);
      if (!pos) return;

      const g = document.createElementNS(svgNS, "g");
      const classes = ["graph-node"];
      if (state.seeds.has(k)) classes.push("is-seed");
      if (state.expansions.has(k)) classes.push("is-expanded");
      if (node.is_external) classes.push("is-external");
      if (node.is_unresolved_placeholder) classes.push("is-unresolved");
      if (state.selectedKey === k) classes.push("is-selected");
      if (state.pathOverlay.nodeIds.has(k)) classes.push("is-path");
      g.setAttribute("class", classes.join(" "));
      g.setAttribute("transform", `translate(${pos.x}, ${pos.y})`);
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "button");
      g.setAttribute("aria-label", `${node.kind} ${node.name}`);

      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("width", String(pos.w));
      rect.setAttribute("height", String(pos.h));
      g.appendChild(rect);

      const title = document.createElementNS(svgNS, "text");
      title.setAttribute("x", "10");
      title.setAttribute("y", "20");
      const shortName = node.name.length > 23 ? `${node.name.slice(0, 22)}…` : node.name;
      title.textContent = shortName;
      g.appendChild(title);

      const sub = document.createElementNS(svgNS, "text");
      sub.setAttribute("x", "10");
      sub.setAttribute("y", "37");
      sub.setAttribute("class", "node-kind-label");
      const flags = [node.kind];
      if (state.seeds.has(k)) flags.push("seed");
      if (state.expansions.has(k)) flags.push("expanded");
      if (node.is_unresolved_placeholder) flags.push("unresolved");
      else if (node.is_external) flags.push("external");
      sub.textContent = flags.join(" · ");
      g.appendChild(sub);

      g.addEventListener("click", (ev) => {
        ev.stopPropagation();
        selectSymbol(k);
      });
      g.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        expandNode(k);
      });
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectSymbol(k);
        }
      });

      nodesGroup.appendChild(g);
    });

    applyViewportTransform();
    el("graph-stats").textContent =
      `${state.activeNodes.length} nodes · ${state.activeEdges.length} edges · ${state.seeds.size} seed(s)`;
    renderSeedPills();
  }

  function renderGraphBanners() {
    const wrap = el("graph-banners");
    wrap.replaceChildren();
    if (state.partialIndex) {
      wrap.appendChild(makeBadge("Partial Index: Incomplete headers or pending TUs", "badge-warn"));
    }
    if (state.truncated) {
      wrap.appendChild(makeBadge("Truncated: Bounded node/edge cap reached", "badge-warn"));
    }
    const nonResolved = state.activeEdges.filter((e) => e.resolution_status !== "resolved").length;
    if (nonResolved > 0) {
      wrap.appendChild(makeBadge(`${nonResolved} Ambiguous / Unresolved edge(s)`, "badge-danger"));
    }
    const eventEdges = state.activeEdges.filter(
      (e) => e.dispatch_kind === "event-registration" || e.kind === "handles_event" || e.kind === "registers_callback"
    ).length;
    if (eventEdges > 0) {
      wrap.appendChild(makeBadge(`${eventEdges} Event-Registration relation(s)`, "badge-event"));
    }
    const virtualEdges = state.activeEdges.filter(
      (e) => e.dispatch_kind === "virtual" || e.dispatch_kind === "indirect" || e.kind === "may_dispatch_to"
    ).length;
    if (virtualEdges > 0) {
      wrap.appendChild(makeBadge(`${virtualEdges} Virtual / Indirect dispatch edge(s)`, "badge-virtual"));
    }
    const unknownNodes = state.activeNodes.filter((n) => n.is_external || n.is_unresolved_placeholder).length;
    if (unknownNodes > 0) {
      wrap.appendChild(makeBadge(`${unknownNodes} External / Unknown symbol(s)`, "badge-neutral"));
    }
  }

  function renderSeedPills() {
    const container = el("seed-pills");
    container.replaceChildren();
    state.seeds.forEach((seedKey) => {
      const node = state.nodeCatalog.get(seedKey);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "badge badge-neutral";
      btn.textContent = `${node ? node.name : seedKey} ×`;
      btn.title = "Remove seed";
      btn.addEventListener("click", () => {
        state.seeds.delete(seedKey);
        state.seedGraphs.delete(seedKey);
        recomputeActiveGraph();
      });
      container.appendChild(btn);
    });
  }

  async function selectSymbol(symKey) {
    state.selectedKey = symKey;
    renderGraph();

    const { contextKey } = getControls();
    let sym = state.nodeCatalog.get(symKey) || normalizeNode({ id: symKey, semantic_key: symKey, name: symKey });

    if (!sym.is_unresolved_placeholder) {
      try {
        const qs = new URLSearchParams({ semantic_key: symKey });
        if (contextKey) qs.set("context_key", contextKey);
        const symData = await apiFetch(`/api/symbol?${qs.toString()}`);
        if (state.selectedKey !== symKey) return;
        if (symData && symData.symbol) {
          sym = normalizeNode({ ...sym, ...symData.symbol });
          state.nodeCatalog.set(symKey, sym);
        }
      } catch (_err) {
        // Keep catalog metadata if symbol detail endpoint reports external/placeholder
      }
    }

    if (state.selectedKey !== symKey) return;
    renderSymbolDetails(sym);
    await loadSnippet(sym);
    renderEvidenceList(symKey);
  }

  function renderSymbolDetails(sym) {
    const card = el("symbol-details");
    const badges = el("symbol-badges");
    const actions = el("symbol-actions");
    badges.replaceChildren();
    card.replaceChildren();

    if (!sym) {
      card.textContent = "No symbol selected.";
      actions.classList.add("hidden");
      return;
    }

    const k = nodeKey(sym);
    badges.appendChild(makeBadge(sym.kind, "badge-kind"));
    if (sym.is_unresolved_placeholder) badges.appendChild(makeBadge("unresolved", "badge-danger"));
    else if (sym.is_external) badges.appendChild(makeBadge("external / unknown", "badge-warn"));
    if (state.expansions.has(k)) badges.appendChild(makeBadge("expanded", "badge-virtual"));

    const addLine = (label, value) => {
      const row = document.createElement("div");
      const strong = document.createElement("strong");
      strong.textContent = `${label}: `;
      const val = document.createElement("span");
      val.textContent = String(value || "—");
      row.append(strong, val);
      card.appendChild(row);
    };

    addLine("Name", sym.name);
    addLine("Qualified", sym.qualified_name);
    addLine("Key", sym.semantic_key);
    if (sym.linkage_scope) addLine("Scope", sym.linkage_scope);

    actions.classList.remove("hidden");
  }

  async function loadSnippet(sym) {
    const meta = el("snippet-meta");
    const codeEl = el("snippet-code");
    codeEl.replaceChildren();

    if (!sym || sym.is_unresolved_placeholder) {
      meta.textContent = "Unresolved target expression — no source file bound.";
      return;
    }

    const { contextKey } = getControls();
    meta.textContent = "Loading source snippet…";
    try {
      const qs = new URLSearchParams({ semantic_key: nodeKey(sym), max_lines: "200" });
      if (contextKey) qs.set("context_key", contextKey);
      const data = await apiFetch(`/api/snippet?${qs.toString()}`);
      if (state.selectedKey !== nodeKey(sym)) return;
      if (data.is_stale || (data.status && data.status !== "ok")) {
        meta.textContent = `Source ${data.status || "stale"}: reindex before retrieving this snippet.`;
        return;
      }
      const content = String(data.content ?? data.snippet ?? data.source ?? "");
      const repoPath = String(data.repo_path || data.file_path || "source");
      const startLine = Number(data.start_line || 1);
      const highlightStart = Number(data.symbol_start_line || data.highlight_start_line || startLine);
      const highlightEnd = Number(data.symbol_end_line || data.highlight_end_line || highlightStart);

      meta.textContent = `${repoPath} (lines ${startLine}–${startLine + content.split("\n").length - 1})${
        data.truncated ? " · truncated" : ""
      }`;

      const lines = content.split(/\r?\n/);
      lines.forEach((lineText, i) => {
        const lineNo = startLine + i;
        const span = document.createElement("span");
        span.className =
          "code-line" + (lineNo >= highlightStart && lineNo <= highlightEnd ? " highlight-line" : "");
        const prefix = String(lineNo).padStart(4, " ") + " | ";
        span.textContent = prefix + lineText;
        codeEl.appendChild(span);
      });
    } catch (err) {
      if (err.name === "AbortError") return;
      meta.textContent = `No indexed source snippet (${err.message})`;
    }
  }

  function renderEvidenceList(symKey) {
    const list = el("evidence-list");
    list.replaceChildren();
    const incident = state.activeEdges.filter((e) => e.source === symKey || e.target === symKey);
    if (incident.length === 0) {
      const li = document.createElement("li");
      li.className = "meta-line";
      li.textContent = "No active relations for this symbol in current view.";
      list.appendChild(li);
      return;
    }

    incident.forEach((edge) => {
      const li = document.createElement("li");
      li.className = "evidence-item";

      const header = document.createElement("div");
      header.className = "section-header-row";
      const dirLabel = edge.source === symKey ? `→ ${edge.target || edge.unresolved_target_expr || "unresolved target"}` : `← ${edge.source}`;
      const title = document.createElement("span");
      title.className = "item-title";
      title.textContent = `${edge.kind} ${dirLabel}`;
      header.appendChild(title);

      const badgeGroup = document.createElement("div");
      badgeGroup.className = "btn-row";
      badgeGroup.appendChild(
        makeBadge(
          edge.dispatch_kind,
          edge.dispatch_kind === "event-registration"
            ? "badge-event"
            : edge.dispatch_kind === "virtual"
            ? "badge-virtual"
            : "badge-neutral"
        )
      );
      badgeGroup.appendChild(
        makeBadge(
          edge.resolution_status,
          edge.resolution_status === "resolved" ? "badge-neutral" : "badge-danger"
        )
      );
      header.appendChild(badgeGroup);
      li.appendChild(header);

      if (edge.evidence_sampled || edge.candidates_sampled) {
        const sampleNote = document.createElement("div");
        sampleNote.className = "item-sub";
        sampleNote.textContent = `Representative evidence/candidates (${edge.indexed_relation_count || "multiple"} indexed occurrences); inspect source for additional sites.`;
        li.appendChild(sampleNote);
      }

      if (edge.reason) {
        const reasonLine = document.createElement("div");
        reasonLine.className = "item-sub";
        reasonLine.textContent = `Reason: ${edge.reason}`;
        li.appendChild(reasonLine);
      }

      edge.evidence.forEach((ev) => {
        const evLine = document.createElement("div");
        evLine.className = "item-sub";
        const loc = ev.repo_path ? `${ev.repo_path}:${ev.start_line || 1}` : `line ${ev.start_line || 1}`;
        evLine.textContent = `Evidence: ${loc}${ev.rule_id ? ` [${ev.rule_id}]` : ""}${ev.notes ? ` — ${ev.notes}` : ""}`;
        li.appendChild(evLine);
      });

      if (edge.candidates && edge.candidates.length > 0) {
        const candLine = document.createElement("div");
        candLine.className = "item-sub";
        const keys = edge.candidates.map((c) => c.candidate_semantic_key || c.semantic_key || c).join(", ");
        candLine.textContent = `Candidates: ${keys}`;
        li.appendChild(candLine);
      }

      list.appendChild(li);
    });
  }

  async function findPath() {
    const source = el("path-source").value.trim();
    const target = el("path-target").value.trim();
    const summary = el("path-summary");
    const stepsEl = el("path-steps");
    stepsEl.replaceChildren();
    if (!source || !target) {
      summary.textContent = "Specify both source and target symbol keys.";
      return;
    }

    const { contextKey, direction, kinds, statuses } = getControls();
    summary.textContent = "Searching bounded path…";
    try {
      const qs = new URLSearchParams({
        source,
        target,
        context_key: contextKey,
        direction,
        max_depth: "6",
      });
      if (kinds.length) qs.set("kinds", kinds.join(","));
      if (statuses.length) qs.set("statuses", statuses.join(","));

      const data = await apiFetch(`/api/path?${qs.toString()}`);
      const slice = registerGraphSlice(data.nodes, data.edges);
      state.pathOverlay = {
        nodeIds: slice.nodeIds,
        edgeIds: slice.edgeIds,
        steps: data.edges || [],
      };
      if (data.truncated) state.truncated = true;
      recomputeActiveGraph();
      fitGraphToView();

      const found = data.found === true || data.status === "found" ||
        (data.found === undefined && slice.edgeIds.size > 0);
      if (!found) {
        summary.textContent = data.truncated ? "Search truncated; a path may still exist." :
          data.incomplete ? "Index incomplete; absence of a path is not established." :
          "No path found within the indexed scope.";
        return;
      }
      summary.textContent = `Found path (${slice.nodeIds.size} nodes, ${slice.edgeIds.size} hops)`;
      (data.edges || []).forEach((edgeRaw, idx) => {
        const e = normalizeEdge(edgeRaw, idx);
        const li = document.createElement("li");
        li.className = "result-item";
        li.textContent = `${idx + 1}. ${e.source} —[${e.kind} / ${e.dispatch_kind}]→ ${e.target}`;
        stepsEl.appendChild(li);
      });
    } catch (err) {
      if (err.name === "AbortError") return;
      summary.textContent = `Path search error: ${err.message}`;
    }
  }

  async function exportContextPack() {
    const statusEl = el("export-status");
    const seeds = state.seeds.size > 0
      ? Array.from(state.seeds)
      : state.selectedKey
      ? [state.selectedKey]
      : [];
    if (seeds.length === 0) {
      statusEl.textContent = "Select or load at least one seed symbol first.";
      return;
    }

    const { contextKey, direction, depth, maxNodes, maxEdges, kinds } = getControls();
    const format = el("export-format").value || "markdown";
    const tokenBudget = Math.max(256, Math.min(64000, parseInt(el("export-budget").value, 10) || 8000));
    const target = el("path-target").value.trim() || null;

    statusEl.textContent = "Generating context pack…";
    try {
      const payload = {
        seeds,
        context_key: contextKey,
        format,
        depth,
        direction,
        kinds: kinds.length ? kinds : null,
        token_budget: tokenBudget,
        max_nodes: maxNodes,
        max_edges: maxEdges,
        target,
      };
      const data = await apiFetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (data.budget_insufficient || data.metadata?.budget_insufficient) {
        statusEl.textContent = "Budget insufficient for the seed/path source. Increase the budget or split the request.";
        return;
      }
      const contentStr =
        typeof data.content === "string" ? data.content : JSON.stringify(data, null, 2);
      const ext = format === "json" ? "json" : "md";
      const mime = format === "json" ? "application/json;charset=utf-8" : "text/markdown;charset=utf-8";
      const blob = new Blob([contentStr], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `legacygraph-context.${ext}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      const metadata = data.metadata || {};
      const estimate = metadata.estimated_tokens ? ` · ~${metadata.estimated_tokens} tokens (estimated)` : "";
      statusEl.textContent = `Downloaded legacygraph-context.${ext}${estimate}${metadata.truncated || data.truncated ? " · omissions recorded" : ""}`;
    } catch (err) {
      if (err.name === "AbortError") return;
      statusEl.textContent = `Export failed: ${err.message}`;
    }
  }

  function applyViewportTransform() {
    el("graph-viewport").setAttribute(
      "transform",
      `translate(${state.panX}, ${state.panY}) scale(${state.scale})`
    );
  }

  function fitGraphToView() {
    if (state.nodePositions.size === 0) return;
    const svg = el("graph-svg");
    const rect = svg.getBoundingClientRect();
    const width = rect.width || 700;
    const height = rect.height || 500;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    state.nodePositions.forEach((p) => {
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x + p.w);
      maxY = Math.max(maxY, p.y + p.h);
    });
    const gw = Math.max(100, maxX - minX + 80);
    const gh = Math.max(100, maxY - minY + 80);
    state.scale = Math.max(0.25, Math.min(1.4, Math.min(width / gw, height / gh)));
    state.panX = Math.round((width - (maxX - minX) * state.scale) / 2 - minX * state.scale);
    state.panY = Math.round((height - (maxY - minY) * state.scale) / 2 - minY * state.scale);
    applyViewportTransform();
  }

  function bindEvents() {
    el("search-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      runSearch(el("search-input").value.trim());
    });
    el("context-select").addEventListener("change", () => {
      resetGraph();
      state.contextKey = el("context-select").value;
      runSearch(el("search-input").value.trim());
    });
    el("wrap-toggle").addEventListener("change", (ev) => {
      el("snippet-pre").classList.toggle("wrap-lines", ev.target.checked);
    });
    el("expand-node-btn").addEventListener("click", () => expandNode(state.selectedKey));
    el("collapse-node-btn").addEventListener("click", () => collapseNode(state.selectedKey));
    el("set-source-btn").addEventListener("click", () => {
      if (state.selectedKey) el("path-source").value = state.selectedKey;
    });
    el("set-target-btn").addEventListener("click", () => {
      if (state.selectedKey) el("path-target").value = state.selectedKey;
    });
    el("find-path-btn").addEventListener("click", findPath);
    el("clear-path-btn").addEventListener("click", () => {
      state.pathOverlay = { nodeIds: new Set(), edgeIds: new Set(), steps: [] };
      el("path-summary").textContent = "";
      el("path-steps").replaceChildren();
      recomputeActiveGraph();
    });
    el("export-btn").addEventListener("click", exportContextPack);

    el("zoom-in-btn").addEventListener("click", () => {
      state.scale = Math.min(2.5, state.scale * 1.2);
      applyViewportTransform();
    });
    el("zoom-out-btn").addEventListener("click", () => {
      state.scale = Math.max(0.2, state.scale / 1.2);
      applyViewportTransform();
    });
    el("fit-graph-btn").addEventListener("click", fitGraphToView);
    el("reload-subgraph-btn").addEventListener("click", reloadGraph);
    ["direction-select", "depth-input", "max-nodes-input", "max-edges-input"].forEach((id) => {
      el(id).addEventListener("change", reloadGraph);
    });
    document.querySelectorAll('input[name="rel-kind"], input[name="res-status"]').forEach((input) => {
      input.addEventListener("change", reloadGraph);
    });
    el("clear-graph-btn").addEventListener("click", resetGraph);

    const svg = el("graph-svg");
    let dragging = false;
    let startX = 0;
    let startY = 0;
    svg.addEventListener("mousedown", (ev) => {
      if (ev.target.closest(".graph-node")) return;
      dragging = true;
      startX = ev.clientX - state.panX;
      startY = ev.clientY - state.panY;
    });
    window.addEventListener("mousemove", (ev) => {
      if (!dragging) return;
      state.panX = ev.clientX - startX;
      state.panY = ev.clientY - startY;
      applyViewportTransform();
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
    });
    svg.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const factor = ev.deltaY < 0 ? 1.1 : 0.9;
        state.scale = Math.max(0.2, Math.min(2.5, state.scale * factor));
        applyViewportTransform();
      },
      { passive: false }
    );
  }

  function resetGraph() {
    state.epoch += 1;
    state.selectedKey = null;
    state.seeds.clear();
    state.seedGraphs.clear();
    state.expansions.clear();
    state.pendingExpansions.clear();
    state.nodeCatalog.clear();
    state.edgeCatalog.clear();
    state.pathOverlay = { nodeIds: new Set(), edgeIds: new Set(), steps: [] };
    state.truncated = false;
    state.partialIndex = false;
    el("path-source").value = "";
    el("path-target").value = "";
    el("path-summary").textContent = "";
    el("path-steps").replaceChildren();
    el("snippet-code").replaceChildren();
    el("snippet-meta").textContent = "Select a symbol to view its indexed source.";
    el("evidence-list").replaceChildren();
    renderSymbolDetails(null);
    showAlert("");
    recomputeActiveGraph();
  }

  async function reloadGraph() {
    const seeds = Array.from(state.seeds);
    if (!seeds.length) return;
    const selected = state.selectedKey;
    state.epoch += 1;
    const epoch = state.epoch;
    state.seedGraphs.clear();
    state.expansions.clear();
    state.pendingExpansions.clear();
    state.pathOverlay = { nodeIds: new Set(), edgeIds: new Set(), steps: [] };
    state.nodeCatalog.clear();
    state.edgeCatalog.clear();
    state.truncated = false;
    recomputeActiveGraph();
    runSearch(el("search-input").value.trim());
    await Promise.all(seeds.map(loadSeedSymbol));
    if (epoch === state.epoch && selected && state.nodeCatalog.has(selected)) await selectSymbol(selected);
  }

  document.addEventListener("DOMContentLoaded", initApp);
})();
