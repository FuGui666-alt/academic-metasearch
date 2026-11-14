(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const form = $("searchForm");
  const qInput = $("q");
  const submitBtn = $("submitBtn");
  const toolbar = $("toolbar");
  const statusEl = $("status");
  const resultsEl = $("results");
  const emptyEl = $("empty");
  const hero = $("hero");
  const sourcesBar = $("sourcesBar");
  const typeFilter = $("typeFilter");
  const oaFilter = $("oaFilter");
  const sortBy = $("sortBy");

  let allResults = [];
  let disabledSources = new Set();
  let loading = false;

  /* ---------- 数据源开关 ---------- */
  async function loadSources() {
    try {
      const res = await fetch("/api/sources");
      const data = await res.json();
      sourcesBar.innerHTML = "";
      data.sources.forEach((s) => {
        const chip = document.createElement("span");
        chip.className = "chip on";
        chip.textContent = s.label || s.name;
        chip.title = `点击切换是否检索 ${s.name}`;
        chip.addEventListener("click", () => {
          if (disabledSources.has(s.name)) {
            disabledSources.delete(s.name);
            chip.classList.add("on");
            chip.classList.remove("off");
          } else {
            disabledSources.add(s.name);
            chip.classList.remove("on");
            chip.classList.add("off");
          }
        });
        sourcesBar.appendChild(chip);
      });
    } catch (e) {
      console.warn("加载数据源失败", e);
    }
  }

  /* ---------- 检索（SSE 流式） ---------- */
  let liveSources = [];

  async function doSearch(query) {
    if (loading) return;
    const q = (query || qInput.value).trim();
    if (!q) return;

    loading = true;
    submitBtn.disabled = true;
    hero.classList.add("shrink");
    emptyEl.innerHTML = "";
    resultsEl.innerHTML = "";
    toolbar.hidden = false;
    allResults = [];
    liveSources = [];
    statusEl.innerHTML = `<span class="spinner"></span>正在并发检索各数据源…`;

    const params = new URLSearchParams({ q, limit: "20" });
    const enabledNames = [...document.querySelectorAll(".chip.on")]
      .map((c) => c.title.replace("点击切换是否检索 ", "").trim())
      .filter(Boolean);
    if (enabledNames.length) params.set("sources", enabledNames.join(","));

    const t0 = performance.now();
    let finished = false;

    try {
      const res = await fetch(`/api/search/stream?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // 按 SSE 双换行切分完整事件
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          handleEvent(block, t0);
        }
      }
      finished = true;
    } catch (e) {
      statusEl.innerHTML = `<span class="bad">检索失败：${escapeHtml(e.message)}</span>`;
      emptyEl.textContent = "请求出错，请检查网络或确认后端服务是否在运行。";
    } finally {
      loading = false;
      submitBtn.disabled = false;
      if (!finished && !allResults.length) {
        emptyEl.textContent = "没有找到结果，换个关键词试试。";
      }
    }
  }

  function handleEvent(block, t0) {
    let event = "message";
    let data = null;
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) {
        try { data = JSON.parse(line.slice(6)); } catch (_) { data = null; }
      }
    }
    if (!data) return;

    if (event === "source") {
      liveSources.push(data);
      renderLiveStatus(t0);
    } else if (event === "partial") {
      allResults = data.results || [];
      renderResults();
      renderLiveStatus(t0);
    } else if (event === "oa") {
      const n = data.open_count || 0;
      if (n > 0) {
        statusEl.insertAdjacentHTML("beforeend",
          ` · <span class="oa-hint">🔓 ${n} 篇有免费全文</span>`);
      }
      // OA 增强现在发生在 done 之后（不阻塞结果展示），事件携带增强后的
      // 全量结果：重渲染卡片，补上「开放获取」标签与全文链接。
      if (data.results) {
        allResults = data.results;
        renderResults();
      }
    } else if (event === "done") {
      allResults = data.results || [];
      liveSources = data.sources || liveSources;
      renderStatus(data);
      renderResults();
      if (!allResults.length) {
        emptyEl.textContent = "没有找到结果，换个关键词试试。";
      }
    } else if (event === "error") {
      statusEl.innerHTML = `<span class="bad">检索出错：${escapeHtml(data.message || "未知错误")}</span>`;
    }
  }

  function renderLiveStatus(t0) {
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    const okCount = liveSources.filter((s) => s.ok).length;
    const got = allResults.length;
    statusEl.innerHTML =
      `<span class="spinner"></span>已返回 <b>${got}</b> 条 · ` +
      `<span class="ok">${okCount}/${liveSources.length}</span> 源完成 · ${secs}s`;
  }

  function renderStatus(data) {
    const parts = [];
    const okCount = (data.sources || []).filter((s) => s.ok).length;
    const total = (data.sources || []).length;
    parts.push(`<span class="ok">${okCount}/${total}</span> 个数据源成功`);
    parts.push(`共 <b>${data.total}</b> 条 · 耗时 ${(data.elapsed_ms / 1000).toFixed(2)}s`);
    const failed = (data.sources || []).filter((s) => !s.ok);
    if (failed.length) {
      const names = failed.map((s) => `${s.label}(${s.error || "失败"})`).join("、");
      parts.push(`<span class="bad">失败：${escapeHtml(names)}</span>`);
    }
    statusEl.innerHTML = parts.join(" · ");
  }

  /* ---------- 渲染 ---------- */
  function filtered() {
    let list = [...allResults];
    const t = typeFilter.value;
    if (t) list = list.filter((r) => r.type === t);
    if (oaFilter.checked) list = list.filter((r) => r.oa_status === "open");
    if (sortBy.value === "year") {
      list.sort((a, b) => (b.year || 0) - (a.year || 0));
    }
    return list;
  }

  function renderResults() {
    const list = filtered();
    resultsEl.innerHTML = "";
    if (!list.length && allResults.length) {
      emptyEl.textContent = "当前筛选条件下没有结果，试试放宽筛选。";
      return;
    }
    emptyEl.textContent = "";
    const frag = document.createDocumentFragment();
    list.forEach((r) => frag.appendChild(card(r)));
    resultsEl.appendChild(frag);
  }

  function card(r) {
    const el = document.createElement("article");
    el.className = "card";

    const titleLink = safeUrl(r.landing_url || r.fulltext_url);
    const h3 = document.createElement("h3");
    if (titleLink) {
      const a = document.createElement("a");
      a.href = titleLink;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = r.title;
      h3.appendChild(a);
    } else {
      h3.textContent = r.title;
    }
    el.appendChild(h3);

    const meta = document.createElement("div");
    meta.className = "meta";
    if (r.authors && r.authors.length) {
      const au = document.createElement("span");
      au.className = "authors";
      au.textContent = r.authors.slice(0, 4).join(", ") +
        (r.authors.length > 4 ? ` 等 ${r.authors.length} 人` : "");
      meta.appendChild(au);
    }
    if (r.venue) meta.appendChild(tag("span", r.venue, "src"));
    if (r.year) meta.appendChild(tag("span", String(r.year), ""));
    if (r.citation_count != null) meta.appendChild(tag("span", `引用 ${r.citation_count}`, ""));
    el.appendChild(meta);

    if (r.abstract) {
      const ab = document.createElement("p");
      ab.className = "abstract";
      ab.textContent = r.abstract;
      el.appendChild(ab);
    }

    const tags = document.createElement("div");
    tags.className = "tags";
    tags.appendChild(tag("span", r.type === "dataset" ? "数据集" : "论文",
      r.type === "dataset" ? "tag type-dataset" : "tag type-paper"));
    if (r.oa_status === "open") {
      const oa = (r.extra || {}).oa || {};
      const label = oa.license ? `开放获取 · ${oa.license}` : "开放获取";
      tags.appendChild(tag("span", label, "tag oa"));
    } else if (r.oa_status === "closed") {
      tags.appendChild(tag("span", "需订阅", "tag closed"));
    }
    (r.sources || []).forEach((s) => tags.appendChild(tag("span", s, "tag src")));

    const actions = document.createElement("div");
    actions.className = "actions";
    if (r.fulltext_url) {
      const b = linkBtn("全文", r.fulltext_url, "btn-link");
      if (b) actions.appendChild(b);
    }
    if (r.doi) {
      // DOI 只保留合法字符（去空白/引号/尖括号），再经 safeUrl 放行
      const doi = String(r.doi).replace(/^https?:\/\/(dx\.)?doi\.org\//, "")
        .replace(/[\s"'<>]/g, "");
      const b = linkBtn("DOI", `https://doi.org/${doi}`, "btn-link ghost");
      if (b) actions.appendChild(b);
    }
    // 知网 / 学校入口：只生成跳转链接，不做抓取
    const links = (r.extra && r.extra.links) || {};
    if (links.cnki_title) {
      const b = linkBtn("知网检索", links.cnki_title, "btn-link ghost");
      if (b) actions.appendChild(b);
    }
    if (links.school) {
      const b = linkBtn("学校", links.school, "btn-link ghost");
      if (b) actions.appendChild(b);
    }
    // 引文网络：需要有 OpenAlex work id
    const workId = pickWorkId(r);
    if (workId) {
      const btn = document.createElement("button");
      btn.className = "btn-link ghost cite-btn";
      btn.type = "button";
      btn.textContent = "引用关系";
      btn.addEventListener("click", () => openCitation(r, workId, btn));
      actions.appendChild(btn);
    }
    tags.appendChild(actions);
    el.appendChild(tags);

    return el;
  }

  function tag(tagName, text, cls) {
    const t = document.createElement(tagName);
    t.className = cls || "tag";
    t.textContent = text;
    return t;
  }

  // N4 加固：外链 scheme 白名单。元数据来自外部 API，理论上可携带
  // javascript: 等伪协议——所有 href 一律先过 safeUrl。
  function safeUrl(u) {
    const s = String(u == null ? "" : u).trim();
    return /^https?:\/\//i.test(s) ? s : "";
  }

  function linkBtn(text, href, cls) {
    const url = safeUrl(href);
    if (!url) return null;
    const a = document.createElement("a");
    a.className = cls;
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = text;
    return a;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    // textContent→innerHTML 只转义 & < >，不转义引号；补上引号转义，
    // 防止 escapeHtml 结果被放进 href="..." 之类的属性插值时逃逸。
    return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ---------- 事件 ---------- */
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    doSearch();
  });
  typeFilter.addEventListener("change", renderResults);
  oaFilter.addEventListener("change", renderResults);
  sortBy.addEventListener("change", renderResults);

  const params = new URLSearchParams(location.search);
  const initialQ = params.get("q");
  /* ---------- 引文网络 ---------- */
  // 从结果里挑一个可用于查引文的标识符
  // 优先 OpenAlex work id（最快），否则退回 DOI（后端会自动解析成 work id）
  function pickWorkId(r) {
    const allIds = r.all_ids || {};
    if (allIds.openalex) return allIds.openalex;

    const lu = String(r.landing_url || "");
    const m = lu.match(/openalex\.org\/(W\d+)/);
    if (m) return m[1];

    if (r.doi) {
      const doi = String(r.doi)
        .replace(/^https?:\/\/(dx\.)?doi\.org\//, "")
        .trim();
      if (doi) return doi;
    }

    // arXiv 结果：用 arXiv ID（后端会转成 10.48550/arxiv.<id> 再解析）
    if (allIds.arxiv) {
      const aid = String(allIds.arxiv)
        .replace(/^https?:\/\/arxiv\.org\/(abs|pdf)\//, "")
        .replace(/\.pdf$/, "")
        .replace(/v\d+$/, "")
        .trim();
      if (/^\d{4}\.\d{4,5}$/.test(aid)) return aid;
    }

    return null;
  }

  let citePanel = null;

  function ensurePanel() {
    if (citePanel) return citePanel;
    citePanel = document.createElement("div");
    citePanel.className = "cite-overlay";
    citePanel.innerHTML = `
      <div class="cite-modal" role="dialog" aria-modal="true">
        <div class="cite-head">
          <div class="cite-title"></div>
          <button class="cite-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="cite-body"></div>
      </div>`;
    document.body.appendChild(citePanel);

    citePanel.querySelector(".cite-close").addEventListener("click", closePanel);
    citePanel.addEventListener("click", (e) => {
      if (e.target === citePanel) closePanel();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePanel();
    });
    return citePanel;
  }

  function closePanel() {
    if (citePanel) citePanel.classList.remove("open");
  }

  async function openCitation(record, workId, btn) {
    const panel = ensurePanel();
    const titleEl = panel.querySelector(".cite-title");
    const bodyEl = panel.querySelector(".cite-body");

    titleEl.textContent = record.title || "";
    bodyEl.innerHTML = `<div class="cite-loading"><span class="spinner"></span>正在获取引用关系…</div>`;
    panel.classList.add("open");
    if (btn) btn.disabled = true;

    try {
      const res = await fetch(`/api/citations?id=${encodeURIComponent(workId)}&limit=25`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      renderCitation(data, bodyEl);
    } catch (e) {
      bodyEl.innerHTML = `<div class="cite-error">获取失败：${escapeHtml(e.message)}</div>`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderCitation(data, bodyEl) {
    const citing = data.citing || [];
    const referenced = data.referenced || [];
    const errors = data.errors || {};

    const section = (label, hint, list, emptyText, errKey) => {
      if (errors[errKey]) {
        return `<section class="cite-sec">
          <h4>${label}</h4>
          <div class="cite-error">${escapeHtml(errors[errKey])}</div>
        </section>`;
      }
      if (!list.length) {
        return `<section class="cite-sec"><h4>${label}</h4>
          <div class="cite-empty">${emptyText}</div></section>`;
      }
      const items = list.map((x) => {
        const link = safeUrl(x.doi || (x.id ? `https://openalex.org/${x.id}` : ""));
        const titleHtml = link
          ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(x.title)}</a>`
          : escapeHtml(x.title);
        const au = (x.authors || []).slice(0, 3).join(", ");
        const more = x.author_count > 3 ? ` 等 ${x.author_count} 人` : "";
        return `<li>
          <div class="cite-item-title">${titleHtml}</div>
          <div class="cite-item-meta">
            ${Number.isFinite(+x.year) ? `<span>${Number(x.year)}</span>` : ""}
            ${x.venue ? `<span>${escapeHtml(x.venue)}</span>` : ""}
            ${Number.isFinite(+x.cited_by) ? `<span>被引 ${Number(x.cited_by)}</span>` : ""}
            ${x.is_oa ? `<span class="oa-mini">开放</span>` : ""}
          </div>
          ${au ? `<div class="cite-item-au">${escapeHtml(au + more)}</div>` : ""}
        </li>`;
      }).join("");
      return `<section class="cite-sec">
        <h4>${label} <span class="cite-count">${list.length}</span>
          <span class="cite-hint">${hint}</span></h4>
        <ul class="cite-list">${items}</ul>
      </section>`;
    };

    bodyEl.innerHTML =
      section("被引 · 谁引用了本文（看最新进展）", "按被引数排序", citing,
        "暂无被引数据（可能是新论文）", "citing") +
      section("引用 · 本文引用了谁（找奠基文献）", "按被引数排序", referenced,
        "暂无引用数据", "referenced");
  }

  loadSources();
  if (initialQ) {
    qInput.value = initialQ;
    doSearch(initialQ);
  }
})();
