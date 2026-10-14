/**
 * dsh-auditrail — client bundle.
 *
 * 入口形态：侧边栏底部的「菜单」按钮（sidebar.footer.action 插槽）。点击弹出
 * 纵向菜单（position:fixed，按按钮视口矩形定位），内含「设置」「切换皮肤」
 * 「Agent测评」三个按钮：「设置」转发点击 shell 原生设置按钮（被 CSS 零尺寸
 * 裁切隐藏、保持挂载）打开原生设置面板；「切换皮肤」转发点击 dsh-skin-nebula
 * 的悬浮按钮（被 CSS 隐藏、保持挂载）并把皮肤菜单重定位到本菜单按钮上方；
 * 「Agent测评」通过 shell.overlay 插槽在中间栏（centerCol）区域打开评测面板
 * （不遮挡侧边栏；Esc 或 ✕ 关闭）。
 *
 * 面板内容：列出本机 DSH 录制的会话，一键导入 auditrail 做确定性审计；
 * 指标直接展示——审计概览、发现、工具调用明细、阶段划分、轨迹预览，
 * 以及（可选）DeepAgent 裁判的 13 维评分。
 *
 * 数据来源：auditrail FastAPI 后端（默认 http://127.0.0.1:18080）。
 *
 * Bundle format: `window.__ModuleLoader__.load({ id, factory })` — the exact
 * shape the client-modules host half serves at `/plugins/<id>/client.js`.
 */
window.__ModuleLoader__.load({
  id: "dsh-auditrail",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    var React = require("react");
    var h = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useCallback = React.useCallback;
    var useRef = React.useRef;

    // =====================================================================
    // constants
    // =====================================================================

    /** Dictionary namespace owned by this plugin. */
    var NS = "auditrail";

    /** Services required by the slot registrations. */
    var inject = ["slots", "locale"];

    /** localStorage key for the connection config. */
    var LS_KEY = "dsh-auditrail:v1";

    /** localStorage key for the remote-agent connection config. */
    var LS_REMOTE_KEY = "dsh-auditrail-remote:v1";

    var DEFAULT_CONFIG = {
      apiBase: "http://127.0.0.1:18080",
      dashboardBase: "http://127.0.0.1:5111",
    };

    /** 自动匹配看板端口时的候选端口（按优先级探测）。 */
    var DASHBOARD_PROBE_PORTS = [5111, 5173, 6173, 4173, 3000];

    /**
     * 评测维度中文标签。key 必须以 auditrail.agents.schemas.DIMENSION_DEFINITIONS
     * 为准（本映射只是展示层副本）。
     */
    var DIMENSION_LABELS = {
      intent_recognition: "意图识别",
      task_planning: "任务规划",
      tool_selection: "工具选择",
      parameter_generation: "参数生成",
      evidence_citation: "证据引用",
      state_tracking: "状态更新",
      final_response: "最终表达",
      context_management: "上下文管理",
      execution_closure: "执行闭环",
      error_recovery: "错误恢复",
      verification: "验证能力",
      safety_permission: "安全与权限",
      efficiency: "成本与效率",
    };

    var STATUS_LABELS = { pass: "通过", partial: "部分", fail: "未通过", unknown: "未评估" };

    var zh = {
      menuButton: "菜单",
      settingsItem: "设置",
      skinItem: "切换皮肤",
      appsItem: "Agent测评",
      panelTitle: "Auditrail · DSH Agent 评测",
      close: "关闭",
      desc: "把本机 DeepSeek Harness 录制的会话导入 auditrail，做确定性工具审计与轨迹分析；指标直接在本页展示。需要先启动 auditrail 后端（auditrail 仓库下 ./start.sh）。",
      apiBase: "后端地址",
      dashboardBase: "看板地址",
      save: "保存",
      testConnection: "测试连接",
      statusOk: "已连接",
      statusFail: "连接失败",
      statusUnknown: "未测试",
      refresh: "刷新",
      loading: "加载中…",
      empty: "未发现 DSH 会话录制（~/.dsh/sessions）。",
      sessionsTitle: "DSH 会话录制",
      runsTitle: "评测结果",
      noRuns: "还没有评测结果，先在上方导入或发起一次评测。",
      colTitle: "会话",
      colWorkspace: "工作区",
      colMessages: "消息",
      colTools: "工具调用",
      colUpdated: "更新时间",
      colAction: "操作",
      colRun: "Run",
      colJudge: "裁判",
      importRun: "导入并审计",
      importing: "导入中…",
      viewMetrics: "查看指标",
      hideMetrics: "收起指标",
      hasJudge: "已裁判",
      noJudge: "未裁判",
      metricsOverview: "审计概览",
      mMessages: "消息",
      mToolCalls: "工具调用",
      mToolResults: "工具结果",
      mMissing: "缺失结果",
      mOrphan: "孤儿结果",
      mFinal: "最终回答字数",
      mTools: "涉及工具",
      findingsTitle: "审计发现",
      noFindings: "无确定性审计发现。",
      phasesTitle: "阶段划分",
      toolCallsTitle: "工具调用明细",
      colToolName: "工具",
      colStatus: "状态",
      colArgs: "参数",
      colResult: "结果",
      trajTitle: "轨迹预览",
      trajShow: "展开轨迹",
      trajHide: "收起轨迹",
      judgeTitle: "DeepAgent 裁判",
      judgeRun: "运行裁判（调用 LLM，耗时较长）",
      judging: "裁判运行中…",
      judgeMissing: "尚未运行裁判。裁判需要模型凭据（在 auditrail 的 .env 中配置）。",
      judgeOverall: "总体结论",
      judgeScore: "完成度得分",
      dimTitle: "评测维度",
      untitled: "(无标题)",
      external: "外部看板",
      remoteTitle: "连接其他 Agent",
      remoteDesc: "对接任何兼容 xuanjian-agent HTTP API 的 agent 服务：导入已有会话做审计，或发起实时对话评测。",
      modeImport: "导入已有会话",
      modeLive: "实时对话评测",
      remoteBaseUrl: "服务地址",
      remoteUserId: "用户 ID",
      remoteSessionId: "会话 ID",
      remoteToken: "Token（可选）",
      remoteModel: "模型（可选）",
      remoteMessage: "测试消息",
      remoteMessagePlaceholder: "输入要发给被测 agent 的任务/问题…",
      importRemote: "导入并审计",
      startLive: "开始评测",
      liveRunning: "评测进行中…",
      liveLog: "进度",
      colSource: "来源",
    };

    var en = {
      menuButton: "Menu",
      settingsItem: "Settings",
      skinItem: "Switch skin",
      appsItem: "Agent Eval",
      panelTitle: "Auditrail · DSH Agent Evaluation",
      close: "Close",
      desc: "Import locally recorded DeepSeek Harness sessions into auditrail for deterministic tool auditing; metrics render right here. Requires the auditrail backend (./start.sh in the auditrail repo).",
      apiBase: "Backend URL",
      dashboardBase: "Dashboard URL",
      save: "Save",
      testConnection: "Test connection",
      statusOk: "Connected",
      statusFail: "Connection failed",
      statusUnknown: "Not tested",
      refresh: "Refresh",
      loading: "Loading…",
      empty: "No DSH session recordings found (~/.dsh/sessions).",
      sessionsTitle: "DSH session recordings",
      runsTitle: "Evaluation results",
      noRuns: "No evaluation runs yet — import one or start a live evaluation above.",
      colTitle: "Session",
      colWorkspace: "Workspace",
      colMessages: "Messages",
      colTools: "Tool calls",
      colUpdated: "Updated",
      colAction: "Action",
      colRun: "Run",
      colJudge: "Judge",
      importRun: "Import & audit",
      importing: "Importing…",
      viewMetrics: "View metrics",
      hideMetrics: "Hide metrics",
      hasJudge: "Judged",
      noJudge: "Not judged",
      metricsOverview: "Audit overview",
      mMessages: "Messages",
      mToolCalls: "Tool calls",
      mToolResults: "Tool results",
      mMissing: "Missing results",
      mOrphan: "Orphan results",
      mFinal: "Final answer chars",
      mTools: "Tools",
      findingsTitle: "Findings",
      noFindings: "No deterministic audit findings.",
      phasesTitle: "Phases",
      toolCallsTitle: "Tool calls",
      colToolName: "Tool",
      colStatus: "Status",
      colArgs: "Arguments",
      colResult: "Result",
      trajTitle: "Trajectory",
      trajShow: "Show trajectory",
      trajHide: "Hide trajectory",
      judgeTitle: "DeepAgent judge",
      judgeRun: "Run judge (calls an LLM, slow)",
      judging: "Judge running…",
      judgeMissing: "Judge has not run yet. It needs model credentials (configured in auditrail's .env).",
      judgeOverall: "Overall",
      judgeScore: "Completion score",
      dimTitle: "Dimensions",
      untitled: "(untitled)",
      external: "External dashboard",
      remoteTitle: "Connect other agents",
      remoteDesc: "Works with any agent service speaking the xuanjian-agent HTTP API: import an existing session for auditing, or run a live chat evaluation.",
      modeImport: "Import session",
      modeLive: "Live evaluation",
      remoteBaseUrl: "Base URL",
      remoteUserId: "User ID",
      remoteSessionId: "Session ID",
      remoteToken: "Token (optional)",
      remoteModel: "Model (optional)",
      remoteMessage: "Test message",
      remoteMessagePlaceholder: "Task or question to send to the agent under test…",
      importRemote: "Import & audit",
      startLive: "Start evaluation",
      liveRunning: "Evaluation running…",
      liveLog: "Progress",
      colSource: "Source",
    };

    // =====================================================================
    // module store: footer button ↔ fullscreen overlay
    // =====================================================================

    var store = { open: false };
    var listeners = [];

    function subscribe(listener) {
      listeners.push(listener);
      return function () {
        listeners = listeners.filter(function (item) { return item !== listener; });
      };
    }

    function setPanelOpen(open) {
      store = { open: Boolean(open) };
      listeners.slice().forEach(function (listener) { listener(store); });
    }

    function usePanelOpen() {
      var pair = useState(store.open);
      var open = pair[0];
      var setOpen = pair[1];
      useEffect(function () {
        return subscribe(function (state) { setOpen(state.open); });
      }, []);
      return open;
    }

    // =====================================================================
    // helpers
    // =====================================================================

    function loadConfig() {
      try {
        var raw = window.localStorage.getItem(LS_KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          return {
            apiBase: String(parsed.apiBase || DEFAULT_CONFIG.apiBase),
            dashboardBase: String(parsed.dashboardBase || DEFAULT_CONFIG.dashboardBase),
          };
        }
      } catch (err) { /* ignore corrupt state */ }
      return { apiBase: DEFAULT_CONFIG.apiBase, dashboardBase: DEFAULT_CONFIG.dashboardBase };
    }

    function saveConfig(cfg) {
      try {
        window.localStorage.setItem(LS_KEY, JSON.stringify(cfg));
      } catch (err) { /* ignore */ }
    }

    /** 远程 agent 连接配置（token 不持久化）。 */
    function loadRemoteConfig() {
      var base = { baseUrl: "", userId: "", sessionId: "", model: "", message: "", token: "" };
      try {
        var raw = window.localStorage.getItem(LS_REMOTE_KEY);
        if (raw) {
          var parsed = JSON.parse(raw);
          Object.keys(base).forEach(function (key) {
            if (typeof parsed[key] === "string") base[key] = parsed[key];
          });
        }
      } catch (err) { /* ignore corrupt state */ }
      return base;
    }

    function saveRemoteConfig(remote) {
      try {
        var persisted = Object.assign({}, remote, { token: "" });
        window.localStorage.setItem(LS_REMOTE_KEY, JSON.stringify(persisted));
      } catch (err) { /* ignore */ }
    }

    /**
     * POST 一个 JSON 并按 SSE 帧（`event: X\ndata: {...}\n\n`）逐条回调。
     * 返回 Promise，流结束或出错时 settle。
     */
    function streamSSE(base, path, payload, onEvent) {
      return fetch(trimSlash(base) + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) {
        if (!res.ok || !res.body) {
          return res.text().then(function (text) {
            var detail = "HTTP " + res.status;
            try {
              var body = JSON.parse(text);
              if (body && body.detail) detail = body.detail;
            } catch (err) { /* non-JSON */ }
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
          });
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";

        function dispatch(raw) {
          var event = "message";
          var dataLines = [];
          raw.split("\n").forEach(function (line) {
            if (line.indexOf("event:") === 0) event = line.slice(6).trim();
            else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
          });
          if (!dataLines.length) return;
          var data = null;
          try { data = JSON.parse(dataLines.join("\n")); } catch (err) { data = dataLines.join("\n"); }
          onEvent(event, data);
        }

        function pump() {
          return reader.read().then(function (result) {
            if (result.done) {
              if (buffer.trim()) dispatch(buffer);
              return undefined;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var idx;
            while ((idx = buffer.indexOf("\n\n")) >= 0) {
              var raw = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              dispatch(raw);
            }
            return pump();
          });
        }

        return pump();
      });
    }

    function trimSlash(url) {
      return String(url || "").replace(/\/+$/, "");
    }

    /**
     * 探测某个 base 是不是 auditrail 看板（Vite dev server）：
     * /api/health 通过代理返回 ok，且 / 返回 text/html（纯后端 18080 的 / 是 404）。
     */
    function probeDashboard(base) {
      var controller = typeof AbortController === "function" ? new AbortController() : null;
      var timer = controller ? setTimeout(function () { controller.abort(); }, 1500) : null;
      var opts = controller ? { signal: controller.signal } : undefined;
      function done(value) {
        if (timer) clearTimeout(timer);
        return value;
      }
      return fetch(trimSlash(base) + "/api/health", opts)
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (body) {
          if (!body || body.status !== "ok") return done(false);
          return fetch(trimSlash(base) + "/", opts).then(function (res) {
            var ct = String(res.headers.get("content-type") || "");
            return done(res.ok && ct.indexOf("text/html") >= 0);
          });
        })
        .catch(function () { return done(false); });
    }

    /**
     * 自动匹配看板地址：先问后端「前端启动时上报的实际地址」（POST /api/dashboard/register
     * 由 vite.config 的 dashboardRegister 插件发出），再依次探测 已保存配置 → 默认 →
     * 常用端口（主机取 apiBase 的）。每个候选都经 probeDashboard 验证确实是看板
     * （/ 返回 text/html），返回第一个在运行的；都没有则返回空串。
     */
    function resolveDashboardBase(cfg) {
      var reported = fetchJSON(cfg.apiBase, "/api/dashboard")
        .then(function (body) { return body && body.url ? String(body.url) : ""; })
        .catch(function () { return ""; });
      return reported.then(function (backendUrl) {
        var candidates = [];
        function add(base) {
          var b = trimSlash(base);
          if (b && candidates.indexOf(b) < 0) candidates.push(b);
        }
        add(backendUrl);
        add(cfg.dashboardBase);
        add(DEFAULT_CONFIG.dashboardBase);
        var host = "127.0.0.1";
        try {
          host = new URL(trimSlash(cfg.apiBase) || DEFAULT_CONFIG.apiBase).hostname || host;
        } catch (err) { /* keep default host */ }
        DASHBOARD_PROBE_PORTS.forEach(function (port) {
          add("http://" + host + ":" + port);
        });
        var chain = Promise.resolve("");
        candidates.forEach(function (base) {
          chain = chain.then(function (found) {
            if (found) return found;
            return probeDashboard(base).then(function (ok) { return ok ? base : ""; });
          });
        });
        return chain;
      });
    }

    function fetchJSON(base, path, options) {
      return fetch(trimSlash(base) + path, options).then(function (res) {
        return res.text().then(function (text) {
          var body = null;
          try { body = text ? JSON.parse(text) : null; } catch (err) { /* non-JSON */ }
          if (!res.ok) {
            var detail = body && body.detail ? body.detail : ("HTTP " + res.status);
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
          }
          return body;
        });
      });
    }

    function formatTime(ms) {
      if (!ms) return "-";
      try { return new Date(Number(ms)).toLocaleString(); } catch (err) { return "-"; }
    }

    function clip(text, max) {
      var value = String(text == null ? "" : text);
      return value.length > max ? value.slice(0, max) + "…" : value;
    }

    function severityColor(severity) {
      if (severity === "error") return "#f85149";
      if (severity === "warning") return "#d29922";
      return "#58a6ff";
    }

    function statusColor(status) {
      if (status === "ok" || status === "pass") return "#3fb950";
      if (status === "error" || status === "fail") return "#f85149";
      if (status === "missing" || status === "partial") return "#d29922";
      return "#9e9e9e";
    }

    /** 2x2 网格图标（菜单里的「应用」项）。 */
    function appsIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": "true" },
        h("rect", { x: 1, y: 1, width: 6, height: 6, rx: 1.5 }),
        h("rect", { x: 9, y: 1, width: 6, height: 6, rx: 1.5 }),
        h("rect", { x: 1, y: 9, width: 6, height: 6, rx: 1.5 }),
        h("rect", { x: 9, y: 9, width: 6, height: 6, rx: 1.5 }));
    }

    /** 评分夹图标（菜单里的「Agent测评」项）。 */
    function evalIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
        h("rect", { x: 3.5, y: 2.5, width: 9, height: 11.5, rx: 1.5 }),
        h("path", { d: "M6 2.5h4" }),
        h("path", { d: "M5.8 8.6l1.5 1.5 3-3.2" }));
    }

    /** 柱状图图标（菜单里的「质量中心」项）。 */
    function qualityIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "aria-hidden": "true" },
        h("path", { d: "M2 13.5V2" }),
        h("path", { d: "M2 13.5h12" }),
        h("path", { d: "M5 11V7.5" }),
        h("path", { d: "M8 11V5" }),
        h("path", { d: "M11 11V8.5" }));
    }

    /** 放大镜图标（菜单里的「RAG 测试」项）。 */
    function ragIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "aria-hidden": "true" },
        h("circle", { cx: 7, cy: 7, r: 4.2 }),
        h("path", { d: "M10.2 10.2L14 14" }));
    }

    /** 代码符号图标（菜单里的「代码分析」项，dsh-code-analysis-platform 注入）。 */
    function codeAnalysisIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
        h("polyline", { points: "5 3 2 8 5 13" }),
        h("polyline", { points: "11 3 14 8 11 13" }),
        h("line", { x1: "9", y1: "2.5", x2: "7", y2: "13.5" }));
    }

    /** 看板列图标（菜单里的「遗留项看板」项，dsh-quality-center 注入）。 */
    function backlogIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": 1.3, "stroke-linecap": "round", "aria-hidden": "true" },
        h("rect", { x: 2.5, y: 3.4, width: 3.1, height: 9.2, rx: 1 }),
        h("rect", { x: 6.45, y: 3.4, width: 3.1, height: 6.6, rx: 1 }),
        h("rect", { x: 10.4, y: 3.4, width: 3.1, height: 4.2, rx: 1 }));
    }

    /** 简化齿轮图标（菜单里的「设置」项）。 */
    function gearIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "aria-hidden": "true" },
        h("circle", { cx: 8, cy: 8, r: 4.4, "stroke-width": 2.2, "stroke-dasharray": "2.1 1.36" }),
        h("circle", { cx: 8, cy: 8, r: 1.7, fill: "currentColor", stroke: "none" }));
    }

    /** 汉堡图标（「菜单」触发按钮）。 */
    function menuIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": "true" },
        h("rect", { x: 2, y: 3.4, width: 12, height: 1.8, rx: 0.9 }),
        h("rect", { x: 2, y: 7.1, width: 12, height: 1.8, rx: 0.9 }),
        h("rect", { x: 2, y: 10.8, width: 12, height: 1.8, rx: 0.9 }));
    }

    /** T 恤图标（菜单里的「切换皮肤」项，与 dsh-skin-nebula 的悬浮按钮一致）。 */
    function shirtIcon(size) {
      var s = size || 16;
      return h("svg", { width: s, height: s, viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" },
        h("path", {
          d: "M16.2 4 20 6.5c.4.3.6.8.4 1.3l-1.2 3c-.2.5-.8.8-1.3.6l-1.4-.5v8.1c0 .6-.4 1-1 1H8.5c-.6 0-1-.4-1-1v-8.1l-1.4.5c-.5.2-1.1-.1-1.3-.6l-1.2-3c-.2-.5 0-1 .4-1.3L7.8 4h2.4c.1 1 .8 1.7 1.8 1.7S13.7 5 13.8 4h2.4Z",
          stroke: "currentColor", "stroke-width": 1.5, "stroke-linejoin": "round",
        }));
    }

    function injectCss() {
      var css = [
        ".al-wrap{display:flex;flex-direction:column;gap:16px;max-width:1000px;margin:0 auto;width:100%}",
        ".al-desc{opacity:.75;font-size:13px;line-height:1.6;margin:0}",
        ".al-card{border:1px solid var(--color-border,rgba(127,127,127,.25));border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:10px}",
        ".al-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}",
        ".al-row label{font-size:12px;opacity:.7;min-width:60px}",
        ".al-row input{flex:1;min-width:200px;padding:5px 8px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px}",
        ".al-btn{padding:5px 12px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}",
        ".al-btn:hover:not(:disabled){background:rgba(127,127,127,.12)}",
        ".al-btn:disabled{opacity:.45;cursor:default}",
        ".al-btn-primary{border-color:var(--color-primary,#4f7cff);color:var(--color-primary,#4f7cff)}",
        ".al-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}",
        ".al-dot-ok{background:#3fb950}",
        ".al-dot-fail{background:#f85149}",
        ".al-dot-unknown{background:#9e9e9e}",
        ".al-status{font-size:12px;opacity:.8}",
        ".al-error{color:#f85149;font-size:12px;white-space:pre-wrap}",
        ".al-table{width:100%;border-collapse:collapse;font-size:12px}",
        ".al-table th{text-align:left;opacity:.6;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--color-border,rgba(127,127,127,.25))}",
        ".al-table td{padding:6px 8px;border-bottom:1px solid var(--color-border,rgba(127,127,127,.12));vertical-align:top}",
        ".al-title{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".al-ws{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.7}",
        ".al-link{color:var(--color-primary,#4f7cff);text-decoration:none;font-size:12px;margin-right:10px;cursor:pointer}",
        ".al-muted{opacity:.6;font-size:12px}",
        ".al-section-title{font-size:13px;font-weight:600;margin:0}",
        ".al-chips{display:flex;gap:8px;flex-wrap:wrap}",
        ".al-chip{border:1px solid var(--color-border,rgba(127,127,127,.25));border-radius:8px;padding:6px 10px;font-size:12px;display:flex;flex-direction:column;gap:2px;min-width:84px}",
        ".al-chip b{font-size:15px}",
        ".al-chip span{opacity:.6}",
        ".al-badge{display:inline-block;padding:1px 7px;border-radius:8px;font-size:11px;border:1px solid}",
        ".al-scroll{max-height:280px;overflow:auto;border:1px solid var(--color-border,rgba(127,127,127,.18));border-radius:8px}",
        ".al-tabs{display:flex;gap:4px;border-bottom:1px solid var(--color-border,rgba(127,127,127,.2));padding-bottom:6px}",
        ".al-tab{padding:4px 12px;border-radius:6px;border:1px solid transparent;background:transparent;color:inherit;font-size:12px;cursor:pointer}",
        ".al-tab:hover{background:rgba(127,127,127,.1)}",
        ".al-tab-active{border-color:var(--color-primary,#4f7cff);color:var(--color-primary,#4f7cff)}",
        ".al-textarea{width:100%;min-height:72px;padding:6px 8px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px;font-family:inherit;resize:vertical;box-sizing:border-box}",
        ".al-log{background:rgba(127,127,127,.08);border-radius:8px;padding:8px 10px;font-family:ui-monospace,monospace;font-size:11px;max-height:160px;overflow:auto;white-space:pre-wrap;line-height:1.6}",
        ".al-scroll .al-table td{border-bottom:1px solid var(--color-border,rgba(127,127,127,.08))}",
        ".al-pre{font-family:ui-monospace,monospace;font-size:11px;white-space:pre-wrap;word-break:break-all;opacity:.85;margin:0}",
        ".al-msg{border-bottom:1px solid var(--color-border,rgba(127,127,127,.1));padding:6px 8px;font-size:12px}",
        ".al-msg-role{display:inline-block;min-width:64px;font-weight:600;margin-right:6px}",
        ".al-dim{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--color-border,rgba(127,127,127,.08));font-size:12px}",
        ".al-dim-label{min-width:88px}",
        ".al-dim-bar{flex:1;height:6px;border-radius:3px;background:rgba(127,127,127,.18);overflow:hidden}",
        ".al-dim-bar-fill{height:100%;border-radius:3px}",
        ".al-dim-score{min-width:36px;text-align:right}",
        /* 侧边栏底部「菜单」按钮与弹出菜单。.al-apps 用 display:contents 不产生
           盒子，只保留 DOM 层级（outside-click 判定）；菜单按钮铺满 footerActions
           宽度（对齐 shell 原设置按钮的几何），折叠 rail 模式收成圆形图标按钮。 */
        ".al-apps{display:contents}",
        ".al-apps-btn{box-sizing:border-box;display:flex;align-items:center;gap:8px;width:calc(100% + 8px);height:34px;margin:4px -4px;padding:6px 2px 6px 10px;border:none;border-radius:12px;background:transparent;color:inherit;font-family:inherit;font-size:14px;line-height:22px;cursor:pointer;overflow:hidden;white-space:nowrap}",
        ".al-apps-btn:hover{background:rgba(127,127,127,.12)}",
        "[class*='collapsed'] .al-apps-btn{width:36px;height:36px;border-radius:50%;justify-content:center;gap:0;padding:0;margin:8px 0 10px}",
        /* 弹出菜单：纵向排列「设置」「Agent测评」两个按钮。position:fixed，
           位置由 JS 按菜单按钮的视口矩形计算（见 MenuFooterAction）。 */
        ".al-menu{position:fixed;display:flex;flex-direction:column;gap:6px;padding:8px;background:var(--dsw-alias-bg-layer-2,Canvas);color:inherit;border:1px solid var(--color-border,rgba(127,127,127,.3));border-radius:14px;box-shadow:0 8px 28px rgba(0,0,0,.28);z-index:1200}",
        ".al-menu-item{display:flex;align-items:center;gap:8px;width:100%;padding:10px 18px;border:none;border-radius:10px;background:rgba(127,127,127,.1);color:inherit;font-size:14px;cursor:pointer;white-space:nowrap;text-align:left}",
        ".al-menu-item:hover{background:rgba(127,127,127,.2)}",
        /* shell 原生设置按钮搬进了上面的菜单条：把 footArea 里的 settingsArea
           压成零尺寸并裁切（不是 display:none——要保持挂载，菜单里的「设置」
           项通过 JS 点击里面的原生按钮来打开设置面板；设置面板是
           position:fixed 覆盖层，不受零尺寸裁切影响）。
           哈希类名按后缀匹配，DSH 升级后若失效只需更新这里。 */
        "[class*='footArea']>[class*='settingsArea']{position:absolute;width:0;height:0;overflow:hidden;padding:0;margin:0}",
        /* 右上角悬浮的「切换皮肤」按钮（dsh-skin-nebula 插件，shell.overlay 层，
           T 恤图标）也搬进本菜单：按钮本体 display:none 保持挂载，菜单里的
           「切换皮肤」项通过 JS 点击它展开皮肤菜单；皮肤菜单容器改为 fixed，
           用 --al-skin-* CSS 变量定位到菜单按钮上方（变量由 JS 设置，见
           MenuFooterAction）。button 前缀是为了压过 nebula 自带的类选择器
           （nebula 渲染的是 <button class="dshSkinSwitcherBtn">）。 */
        "button.dshSkinSwitcherBtn{display:none}",
        "div.dshSkinSwitcher{position:fixed;top:auto;right:auto;left:var(--al-skin-left,56px);bottom:var(--al-skin-bottom,84px);align-items:flex-start}",
        /* 覆盖层（shell.overlay 层默认 click-through，需要恢复指针事件）。
           位置/尺寸由内联样式给定：只覆盖中间栏（centerCol）。 */
        ".al-overlay{position:fixed;z-index:1100;pointer-events:auto;background:var(--color-background,Canvas);color:var(--color-text,CanvasText);display:flex;flex-direction:column;border-left:1px solid var(--color-border,rgba(127,127,127,.25))}",
        ".al-overlay-header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--color-border,rgba(127,127,127,.25))}",
        ".al-overlay-title{font-size:15px;font-weight:600;margin:0;flex:1}",
        ".al-overlay-close{border:none;background:transparent;color:inherit;font-size:18px;cursor:pointer;padding:4px 10px;border-radius:6px;line-height:1}",
        ".al-overlay-close:hover{background:rgba(127,127,127,.12)}",
        ".al-overlay-body{flex:1;overflow:auto;padding:20px}",
      ].join("\n");
      var tag = window.document.createElement("style");
      tag.textContent = css;
      tag.setAttribute("data-dsh-auditrail", "1");
      window.document.head.appendChild(tag);
      return tag;
    }

    // =====================================================================
    // metrics sub-views
    // =====================================================================

    function chip(label, value) {
      return h("div", { className: "al-chip", key: label },
        h("b", null, String(value)),
        h("span", null, label));
    }

    function AuditOverview(props) {
      var audit = props.audit || {};
      var t = props.t;
      return h("div", null,
        h("p", { className: "al-section-title" }, t("metricsOverview")),
        h("div", { className: "al-chips" },
          chip(t("mMessages"), audit.message_count != null ? audit.message_count : "-"),
          chip(t("mToolCalls"), audit.tool_call_count != null ? audit.tool_call_count : "-"),
          chip(t("mToolResults"), audit.tool_result_count != null ? audit.tool_result_count : "-"),
          chip(t("mMissing"), (audit.missing_tool_results || []).length),
          chip(t("mOrphan"), (audit.orphan_tool_results || []).length),
          chip(t("mFinal"), audit.final_answer_chars != null ? audit.final_answer_chars : "-"),
        ),
        (audit.tool_names || []).length
          ? h("p", { className: "al-muted" }, t("mTools") + ": " + audit.tool_names.join(", "))
          : null,
      );
    }

    function Findings(props) {
      var findings = props.findings || [];
      var t = props.t;
      return h("div", null,
        h("p", { className: "al-section-title" }, t("findingsTitle") + " (" + findings.length + ")"),
        findings.length === 0
          ? h("p", { className: "al-muted" }, t("noFindings"))
          : h("div", { className: "al-scroll" }, findings.map(function (f, i) {
              return h("div", { className: "al-msg", key: i },
                h("span", { className: "al-badge", style: { color: severityColor(f.severity), borderColor: severityColor(f.severity) } }, f.severity || "info"),
                h("b", null, " " + (f.code || "") + " "),
                h("span", { className: "al-muted" }, clip(f.message, 300)));
            })),
      );
    }

    function Phases(props) {
      var phases = props.phases || [];
      if (!phases.length) return null;
      return h("div", null,
        h("p", { className: "al-section-title" }, props.t("phasesTitle")),
        h("p", { className: "al-muted" },
          phases.map(function (p, i) {
            return (i + 1) + ". " + p.phase + " (" + (p.tool_count || 0) + ")";
          }).join("  →  ")),
      );
    }

    function ToolCalls(props) {
      var calls = props.toolCalls || [];
      var t = props.t;
      if (!calls.length) return null;
      return h("div", null,
        h("p", { className: "al-section-title" }, t("toolCallsTitle") + " (" + calls.length + ")"),
        h("div", { className: "al-scroll" },
          h("table", { className: "al-table" },
            h("thead", null, h("tr", null,
              h("th", null, "#"),
              h("th", null, t("colToolName")),
              h("th", null, t("colStatus")),
              h("th", null, t("colArgs")),
              h("th", null, t("colResult")),
            )),
            h("tbody", null, calls.map(function (c, i) {
              return h("tr", { key: i },
                h("td", null, String(c.index != null ? c.index : i)),
                h("td", null, h("code", null, c.tool || "unknown")),
                h("td", null, h("span", { className: "al-badge", style: { color: statusColor(c.status), borderColor: statusColor(c.status) } }, c.status || "?")),
                h("td", null, h("p", { className: "al-pre" }, clip(c.args_preview, 120))),
                h("td", null, h("p", { className: "al-pre" }, clip(c.result_preview, 160))));
            })),
          )),
      );
    }

    function Trajectory(props) {
      var messages = props.messages || [];
      if (!messages.length) return null;
      return h("div", { className: "al-scroll", style: { maxHeight: "none" } }, messages.map(function (m, i) {
        var role = m.role || "?";
        var text = m.content || "";
        if (m.tool_calls && m.tool_calls.length) {
          text = (text ? text + " " : "") + "[" + m.tool_calls.map(function (c) { return c.name || "?"; }).join(", ") + "]";
        }
        return h("div", { className: "al-msg", key: i },
          h("span", { className: "al-msg-role", style: { color: role === "assistant" ? "#4f7cff" : role === "tool" ? "#d29922" : "inherit" } }, role),
          h("span", null, clip(text, 400)));
      }));
    }

    function DimensionRow(props) {
      var dimKey = props.dimKey;
      var dim = props.dim || {};
      var label = DIMENSION_LABELS[dimKey] || dimKey;
      var status = dim.status || "unknown";
      var score = typeof dim.score === "number" ? dim.score : null;
      var color = statusColor(status);
      return h("div", { className: "al-dim" },
        h("span", { className: "al-dim-label" }, label),
        h("span", { className: "al-badge", style: { color: color, borderColor: color } }, STATUS_LABELS[status] || status),
        h("span", { className: "al-dim-bar" },
          score != null
            ? h("span", { className: "al-dim-bar-fill", style: { width: Math.max(0, Math.min(100, score)) + "%", background: color, display: "block" } })
            : null),
        h("span", { className: "al-dim-score" }, score != null ? String(score) : "-"),
        h("span", { className: "al-muted", title: dim.summary || "" }, clip(dim.summary, 60)),
      );
    }

    function JudgePanel(props) {
      var judge = props.judge;
      var t = props.t;
      if (!judge || judge.status === "missing") {
        return h("div", null,
          h("p", { className: "al-section-title" }, t("judgeTitle")),
          h("p", { className: "al-muted" }, t("judgeMissing")),
          h("div", { className: "al-row" },
            h("button", { className: "al-btn al-btn-primary", disabled: props.judging, onClick: props.onRunJudge },
              props.judging ? t("judging") : t("judgeRun"))),
        );
      }
      var dimensions = judge.dimensions || {};
      var dimKeys = Object.keys(dimensions);
      return h("div", null,
        h("p", { className: "al-section-title" }, t("judgeTitle")),
        h("div", { className: "al-chips" },
          chip(t("judgeOverall"), STATUS_LABELS[judge.overall_status] || judge.overall_status || "-"),
          chip(t("judgeScore"), judge.completion_score != null ? judge.completion_score : "-"),
        ),
        judge.summary ? h("p", { className: "al-muted" }, clip(judge.summary, 400)) : null,
        dimKeys.length
          ? h("div", null,
              h("p", { className: "al-section-title" }, t("dimTitle")),
              dimKeys.map(function (key) {
                return h(DimensionRow, { key: key, dimKey: key, dim: dimensions[key] });
              }))
          : null,
      );
    }

    // =====================================================================
    // panel content (config + sessions + runs + metrics detail)
    // =====================================================================

    function AuditrailPanel(props) {
      var t = props.t;

      var cfgPair = useState(loadConfig);
      var cfg = cfgPair[0];
      var setCfg = cfgPair[1];

      var healthPair = useState("unknown");
      var health = healthPair[0];
      var setHealth = healthPair[1];

      var sessionsPair = useState(null);
      var sessions = sessionsPair[0];
      var setSessions = sessionsPair[1];

      var runsPair = useState(null);
      var runs = runsPair[0];
      var setRuns = runsPair[1];

      var busyPair = useState(false);
      var busy = busyPair[0];
      var setBusy = busyPair[1];

      var errPair = useState("");
      var error = errPair[0];
      var setError = errPair[1];

      var importingPair = useState("");
      var importing = importingPair[0];
      var setImporting = importingPair[1];

      var selectedPair = useState("");
      var selectedRun = selectedPair[0];
      var setSelectedRun = selectedPair[1];

      var detailPair = useState(null);
      var detail = detailPair[0];
      var setDetail = detailPair[1];

      var trajPair = useState(false);
      var trajOpen = trajPair[0];
      var setTrajOpen = trajPair[1];

      var judgingPair = useState(false);
      var judging = judgingPair[0];
      var setJudging = judgingPair[1];

      // ---- 连接其他 Agent ----
      var remotePair = useState(loadRemoteConfig);
      var remote = remotePair[0];
      var setRemote = remotePair[1];

      var modePair = useState("import");
      var remoteMode = modePair[0];
      var setRemoteMode = modePair[1];

      var remoteBusyPair = useState(false);
      var remoteBusy = remoteBusyPair[0];
      var setRemoteBusy = remoteBusyPair[1];

      var liveLogPair = useState([]);
      var liveLog = liveLogPair[0];
      var setLiveLog = liveLogPair[1];

      function updateRemote(patch) {
        setRemote(function (prev) {
          var next = Object.assign({}, prev, patch);
          saveRemoteConfig(next);
          return next;
        });
      }

      function selectRunAndShow(runId) {
        if (!runId) return;
        setSelectedRun(runId);
        setDetail(null);
        setTrajOpen(false);
        loadDetail(runId, false, null).catch(function (err) { setError(String(err && err.message || err)); });
      }

      function onImportRemote() {
        if (!remote.baseUrl || !remote.userId || !remote.sessionId) {
          setError(t("remoteBaseUrl") + " / " + t("remoteUserId") + " / " + t("remoteSessionId") + " ?");
          return;
        }
        setRemoteBusy(true);
        setError("");
        fetchJSON(cfg.apiBase, "/api/runs/import-session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url: remote.baseUrl,
            user_id: remote.userId,
            session_id: remote.sessionId,
            token: remote.token || undefined,
          }),
        }).then(function (body) {
          return loadRuns().then(function () {
            var runId = body && body.run && body.run.name;
            selectRunAndShow(runId);
          });
        }).catch(function (err) {
          setError(String(err && err.message || err));
        }).then(function () {
          setRemoteBusy(false);
        });
      }

      function appendLiveLog(line) {
        setLiveLog(function (prev) { return prev.concat([line]).slice(-80); });
      }

      function onStartLive() {
        if (!remote.baseUrl || !remote.userId || !remote.message) {
          setError(t("remoteBaseUrl") + " / " + t("remoteUserId") + " / " + t("remoteMessage") + " ?");
          return;
        }
        setRemoteBusy(true);
        setError("");
        setLiveLog([]);
        streamSSE(cfg.apiBase, "/api/live/evaluate", {
          base_url: remote.baseUrl,
          user_id: remote.userId,
          message: remote.message,
          model: remote.model || undefined,
          token: remote.token || undefined,
        }, function (event, data) {
          if (event === "remote_step") {
            appendLiveLog((data.status === "started" ? "▸ " : "✓ ") + (data.stage || ""));
          } else if (event === "run_started") {
            appendLiveLog("▸ run_started " + (data.session_id || ""));
          } else if (event === "run_finished") {
            appendLiveLog("✓ run_finished");
            var runId = data && data.run && data.run.name;
            loadRuns().then(function () { selectRunAndShow(runId); });
          } else if (event === "error") {
            appendLiveLog("✗ " + (data.detail || "error"));
            setError(String(data.detail || "error"));
          }
        }).catch(function (err) {
          setError(String(err && err.message || err));
        }).then(function () {
          setRemoteBusy(false);
        });
      }

      function renderRemoteAgent() {
        return h("div", { className: "al-card" },
          h("p", { className: "al-section-title" }, t("remoteTitle")),
          h("p", { className: "al-muted" }, t("remoteDesc")),
          h("div", { className: "al-tabs" },
            h("button", {
              className: "al-tab" + (remoteMode === "import" ? " al-tab-active" : ""),
              onClick: function () { setRemoteMode("import"); },
            }, t("modeImport")),
            h("button", {
              className: "al-tab" + (remoteMode === "live" ? " al-tab-active" : ""),
              onClick: function () { setRemoteMode("live"); },
            }, t("modeLive"))),
          h("div", { className: "al-row" },
            h("label", null, t("remoteBaseUrl")),
            h("input", {
              value: remote.baseUrl,
              placeholder: "http://host:8788",
              onChange: function (e) { updateRemote({ baseUrl: e.target.value }); },
            })),
          h("div", { className: "al-row" },
            h("label", null, t("remoteUserId")),
            h("input", {
              value: remote.userId,
              onChange: function (e) { updateRemote({ userId: e.target.value }); },
            })),
          remoteMode === "import"
            ? h("div", { className: "al-row" },
                h("label", null, t("remoteSessionId")),
                h("input", {
                  value: remote.sessionId,
                  onChange: function (e) { updateRemote({ sessionId: e.target.value }); },
                }))
            : null,
          remoteMode === "live"
            ? h("div", { className: "al-row" },
                h("label", null, t("remoteModel")),
                h("input", {
                  value: remote.model,
                  onChange: function (e) { updateRemote({ model: e.target.value }); },
                }))
            : null,
          remoteMode === "live"
            ? h("textarea", {
                className: "al-textarea",
                placeholder: t("remoteMessagePlaceholder"),
                value: remote.message,
                onChange: function (e) { updateRemote({ message: e.target.value }); },
              })
            : null,
          h("div", { className: "al-row" },
            h("label", null, t("remoteToken")),
            h("input", {
              value: remote.token,
              type: "password",
              onChange: function (e) { updateRemote({ token: e.target.value }); },
            })),
          h("div", { className: "al-row" },
            remoteMode === "import"
              ? h("button", { className: "al-btn al-btn-primary", disabled: remoteBusy, onClick: onImportRemote },
                  remoteBusy ? t("importing") : t("importRemote"))
              : h("button", { className: "al-btn al-btn-primary", disabled: remoteBusy, onClick: onStartLive },
                  remoteBusy ? t("liveRunning") : t("startLive"))),
          liveLog.length
            ? h("div", null,
                h("p", { className: "al-section-title" }, t("liveLog")),
                h("div", { className: "al-log" }, liveLog.join("\n")))
            : null,
        );
      }

      var loadSessions = useCallback(function () {
        return fetchJSON(cfg.apiBase, "/api/dsh/sessions?limit=50").then(function (body) {
          setSessions((body && body.sessions) || []);
        });
      }, [cfg.apiBase]);

      var loadRuns = useCallback(function () {
        return fetchJSON(cfg.apiBase, "/api/runs").then(function (body) {
          var items = ((body && body.runs) || []).slice();
          items.sort(function (a, b) { return String(b.name).localeCompare(String(a.name)); });
          setRuns(items);
        });
      }, [cfg.apiBase]);

      var refreshAll = useCallback(function () {
        setBusy(true);
        setError("");
        return Promise.all([loadSessions(), loadRuns()])
          .catch(function (err) { setError(String(err && err.message || err)); })
          .then(function () { setBusy(false); });
      }, [loadSessions, loadRuns]);

      var checkHealth = useCallback(function () {
        setError("");
        return fetchJSON(cfg.apiBase, "/api/health").then(function (body) {
          var ok = body && body.status === "ok";
          setHealth(ok ? "ok" : "fail");
          return ok;
        }).catch(function (err) {
          setHealth("fail");
          setError(String(err && err.message || err));
          return false;
        });
      }, [cfg.apiBase]);

      useEffect(function () {
        var cancelled = false;
        fetchJSON(cfg.apiBase, "/api/health").then(function (body) {
          var ok = body && body.status === "ok";
          if (cancelled) return;
          setHealth(ok ? "ok" : "fail");
          if (!ok) return;
          refreshAll();
          // 自动匹配看板实际端口：后端注册值优先，失败时探测候选端口。
          resolveDashboardBase(cfg).then(function (resolved) {
            if (cancelled || !resolved) return;
            if (trimSlash(resolved) === trimSlash(cfg.dashboardBase)) return;
            var next = { apiBase: cfg.apiBase, dashboardBase: resolved };
            setCfg(next);
            saveConfig(next);
          });
        }).catch(function () {
          if (!cancelled) setHealth("fail");
        });
        return function () { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);

      function loadDetail(runId, withTrajectory, prevDetail) {
        var calls = [
          fetchJSON(cfg.apiBase, "/api/runs/" + encodeURIComponent(runId) + "/audit"),
          fetchJSON(cfg.apiBase, "/api/runs/" + encodeURIComponent(runId) + "/judge"),
        ];
        if (withTrajectory) {
          calls.push(fetchJSON(cfg.apiBase, "/api/runs/" + encodeURIComponent(runId) + "/trajectory"));
        }
        return Promise.all(calls).then(function (results) {
          setDetail({
            run_id: runId,
            audit: results[0],
            judge: results[1],
            trajectory: withTrajectory
              ? ((results[2] && results[2].messages) || [])
              : (prevDetail && prevDetail.run_id === runId ? prevDetail.trajectory : null),
          });
        });
      }

      function onSelectRun(runId) {
        if (selectedRun === runId) {
          setSelectedRun("");
          setDetail(null);
          setTrajOpen(false);
          return;
        }
        setSelectedRun(runId);
        setDetail(null);
        setTrajOpen(false);
        setError("");
        loadDetail(runId, false, null).catch(function (err) { setError(String(err && err.message || err)); });
      }

      function onToggleTrajectory() {
        if (trajOpen) {
          setTrajOpen(false);
          return;
        }
        setTrajOpen(true);
        if (detail && detail.trajectory) return;
        setError("");
        loadDetail(selectedRun, true, detail).catch(function (err) { setError(String(err && err.message || err)); });
      }

      function onImport(item) {
        setImporting(item.session_id);
        setError("");
        fetchJSON(cfg.apiBase, "/api/dsh/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: item.session_id, workspace: item.workspace_dir }),
        }).then(function (body) {
          return loadRuns().then(function () {
            setSelectedRun(body.run_id);
            setDetail(null);
            setTrajOpen(false);
            return loadDetail(body.run_id, false, null);
          });
        }).catch(function (err) {
          setError(String(err && err.message || err));
        }).then(function () {
          setImporting("");
        });
      }

      function onRunJudge() {
        if (!selectedRun) return;
        setJudging(true);
        setError("");
        fetchJSON(cfg.apiBase, "/api/runs/" + encodeURIComponent(selectedRun) + "/judge", { method: "POST" })
          .then(function () { return loadDetail(selectedRun, Boolean(trajOpen), detail); })
          .then(function () { return loadRuns(); })
          .catch(function (err) { setError(String(err && err.message || err)); })
          .then(function () { setJudging(false); });
      }

      // ------------------------- renderers -------------------------

      function renderHealth() {
        var cls = health === "ok" ? "al-dot al-dot-ok" : health === "fail" ? "al-dot al-dot-fail" : "al-dot al-dot-unknown";
        var label = health === "ok" ? t("statusOk") : health === "fail" ? t("statusFail") : t("statusUnknown");
        return h("span", { className: "al-status" }, h("span", { className: cls }), label);
      }

      function renderConfig() {
        return h("div", { className: "al-card" },
          h("div", { className: "al-row" },
            h("label", null, t("apiBase")),
            h("input", {
              value: cfg.apiBase,
              onChange: function (e) { setCfg({ apiBase: e.target.value, dashboardBase: cfg.dashboardBase }); },
            }),
          ),
          h("div", { className: "al-row" },
            h("label", null, t("dashboardBase")),
            h("input", {
              value: cfg.dashboardBase,
              onChange: function (e) { setCfg({ apiBase: cfg.apiBase, dashboardBase: e.target.value }); },
            }),
          ),
          h("div", { className: "al-row" },
            h("button", { className: "al-btn", onClick: function () { saveConfig(cfg); } }, t("save")),
            h("button", {
              className: "al-btn",
              onClick: function () { saveConfig(cfg); checkHealth().then(function (ok) { if (ok) refreshAll(); }); },
            }, t("testConnection")),
            renderHealth(),
          ),
        );
      }

      function renderSessions() {
        var body;
        if (!sessions) body = h("p", { className: "al-muted" }, t("loading"));
        else if (!sessions.length) body = h("p", { className: "al-muted" }, t("empty"));
        else body = h("div", { className: "al-scroll" },
          h("table", { className: "al-table" },
            h("thead", null, h("tr", null,
              h("th", null, t("colTitle")),
              h("th", null, t("colWorkspace")),
              h("th", null, t("colMessages")),
              h("th", null, t("colTools")),
              h("th", null, t("colUpdated")),
              h("th", null, t("colAction")),
            )),
            h("tbody", null, sessions.map(function (item) {
              var isImporting = importing === item.session_id;
              return h("tr", { key: item.session_id },
                h("td", null, h("div", { className: "al-title", title: item.title || "" }, item.title || t("untitled"))),
                h("td", null, h("div", { className: "al-ws", title: item.workspace || "" }, item.workspace || item.workspace_dir || "-")),
                h("td", null, String(item.user_message_count || 0) + " / " + String(item.assistant_message_count || 0)),
                h("td", null, String(item.tool_call_count || 0)),
                h("td", null, formatTime(item.updated_at)),
                h("td", null, h("button", {
                  className: "al-btn al-btn-primary",
                  disabled: Boolean(importing),
                  onClick: function () { onImport(item); },
                }, isImporting ? t("importing") : t("importRun"))),
              );
            })),
          ));
        return h("div", { className: "al-card" },
          h("div", { className: "al-row" },
            h("p", { className: "al-section-title", style: { flex: 1 } }, t("sessionsTitle")),
            h("button", { className: "al-btn", onClick: refreshAll, disabled: busy }, t("refresh")),
          ),
          body,
        );
      }

      function renderRuns() {
        var body;
        if (!runs) body = h("p", { className: "al-muted" }, t("loading"));
        else if (!runs.length) body = h("p", { className: "al-muted" }, t("noRuns"));
        else body = h("div", { className: "al-scroll" },
          h("table", { className: "al-table" },
            h("thead", null, h("tr", null,
              h("th", null, t("colRun")),
              h("th", null, t("colSource")),
              h("th", null, t("colJudge")),
              h("th", null, t("colAction")),
            )),
            h("tbody", null, runs.map(function (item) {
              var runId = String(item.name || "");
              var isSelected = selectedRun === runId;
              var isDsh = item.source_base_url === "dsh://local";
              return h("tr", { key: runId },
                h("td", null, h("div", { className: "al-title", title: runId }, runId)),
                h("td", null, h("span", {
                  className: "al-badge",
                  title: item.source_base_url || "",
                  style: isDsh
                    ? { color: "#4f7cff", borderColor: "#4f7cff" }
                    : { color: "#9e9e9e", borderColor: "#9e9e9e" },
                }, isDsh ? "DSH" : (item.server_host || "remote"))),
                h("td", null, h("span", {
                  className: "al-badge",
                  style: item.has_judge
                    ? { color: "#3fb950", borderColor: "#3fb950" }
                    : { color: "#9e9e9e", borderColor: "#9e9e9e" },
                }, item.has_judge ? t("hasJudge") : t("noJudge"))),
                h("td", null,
                  h("a", { className: "al-link", onClick: function () { onSelectRun(runId); } },
                    isSelected ? t("hideMetrics") : t("viewMetrics")),
                  h("a", {
                    className: "al-link",
                    href: trimSlash(cfg.dashboardBase) + "/runs/" + encodeURIComponent(runId),
                    target: "_blank",
                    rel: "noreferrer",
                  }, t("external"))),
              );
            })),
          ));
        return h("div", { className: "al-card" },
          h("p", { className: "al-section-title" }, t("runsTitle")),
          body,
        );
      }

      function renderDetail() {
        if (!selectedRun) return null;
        if (!detail || detail.run_id !== selectedRun) {
          return h("div", { className: "al-card" }, h("p", { className: "al-muted" }, t("loading")));
        }
        var audit = detail.audit || {};
        return h("div", { className: "al-card" },
          h("div", { className: "al-row" },
            h("p", { className: "al-section-title", style: { flex: 1 } }, selectedRun),
            h("button", { className: "al-btn", onClick: onToggleTrajectory },
              trajOpen ? t("trajHide") : t("trajShow")),
          ),
          h(AuditOverview, { audit: audit, t: t }),
          h(Phases, { phases: audit.phases, t: t }),
          h(Findings, { findings: audit.findings, t: t }),
          h(ToolCalls, { toolCalls: audit.tool_calls, t: t }),
          h(JudgePanel, { judge: detail.judge, judging: judging, onRunJudge: onRunJudge, t: t }),
          trajOpen
            ? h("div", null,
                h("p", { className: "al-section-title" }, t("trajTitle")),
                detail.trajectory
                  ? h(Trajectory, { messages: detail.trajectory })
                  : h("p", { className: "al-muted" }, t("loading")))
            : null,
        );
      }

      return h("div", { className: "al-wrap" },
        h("p", { className: "al-desc" }, t("desc")),
        renderConfig(),
        error ? h("div", { className: "al-error" }, error) : null,
        renderSessions(),
        renderRemoteAgent(),
        renderRuns(),
        renderDetail(),
      );
    }

    // =====================================================================
    // sidebar footer 「菜单」button + popup menu（设置 / 切换皮肤 / Agent测评）
    // =====================================================================

    /**
     * 打开 shell 原生设置面板：footArea 里的原生设置按钮被 CSS 视觉隐藏
     * （零尺寸裁切）但保持挂载，这里转发一次点击。
     */
    function openShellSettings() {
      var btn = window.document.querySelector("[class*='footArea'] button[class*='trigger']");
      if (btn) btn.click();
    }

    /** dsh-skin-nebula 的悬浮「切换皮肤」按钮（被 CSS 隐藏、保持挂载）。 */
    function findSkinSwitcherBtn() {
      return window.document.querySelector(".dshSkinSwitcherBtn");
    }

    function MenuFooterAction(props) {
      var t = props.t;
      var wide = !props || props.wide !== false;
      var menuPair = useState(false);
      var menuOpen = menuPair[0];
      var setMenuOpen = menuPair[1];
      var rootRef = useRef(null);
      var btnRef = useRef(null);

      // 菜单用 position:fixed（.al-apps 是 display:contents，没有可定位的盒子），
      // 打开期间按按钮的视口矩形定位（按钮上方、左对齐），并跟踪窗口尺寸变化。
      var posPair = useState(null);
      var menuPos = posPair[0];
      var setMenuPos = posPair[1];

      useEffect(function () {
        if (!menuOpen) return undefined;
        function updateMenuPos() {
          var btn = btnRef.current;
          if (!btn) return;
          var r = btn.getBoundingClientRect();
          setMenuPos({
            bottom: Math.max(0, window.innerHeight - r.top + 8) + "px",
            left: Math.max(0, r.left) + "px",
          });
        }
        updateMenuPos();
        window.addEventListener("resize", updateMenuPos);
        return function () {
          window.removeEventListener("resize", updateMenuPos);
        };
      }, [menuOpen]);

      useEffect(function () {
        if (!menuOpen) return undefined;
        function onDocMouseDown(event) {
          if (rootRef.current && !rootRef.current.contains(event.target)) {
            setMenuOpen(false);
          }
        }
        function onKeyDown(event) {
          if (event.key === "Escape") setMenuOpen(false);
        }
        window.document.addEventListener("mousedown", onDocMouseDown);
        window.document.addEventListener("keydown", onKeyDown);
        return function () {
          window.document.removeEventListener("mousedown", onDocMouseDown);
          window.document.removeEventListener("keydown", onKeyDown);
        };
      }, [menuOpen]);

      // 展开皮肤菜单：先把 --al-skin-* 变量对齐到菜单按钮上方，再转发点击
      // 被隐藏的 nebula 悬浮按钮（它的内部 open 状态只能由它自己翻转）。
      function openSkinSwitcher() {
        var skinBtn = findSkinSwitcherBtn();
        if (!skinBtn) return;
        var anchor = btnRef.current;
        if (anchor) {
          var r = anchor.getBoundingClientRect();
          var rootStyle = window.document.documentElement.style;
          rootStyle.setProperty("--al-skin-left", Math.max(0, r.left) + "px");
          rootStyle.setProperty("--al-skin-bottom", Math.max(0, window.innerHeight - r.top + 8) + "px");
        }
        skinBtn.click();
      }

      return h("div", { className: "al-apps", ref: rootRef },
        h("button", {
          className: "al-apps-btn",
          ref: btnRef,
          title: t("menuButton"),
          "aria-label": t("menuButton"),
          "aria-haspopup": "menu",
          "aria-expanded": menuOpen ? "true" : "false",
          onClick: function () { setMenuOpen(!menuOpen); },
        },
          menuIcon(16),
          wide ? h("span", null, t("menuButton")) : null),
        menuOpen
          ? h("div", { className: "al-menu", style: menuPos || undefined, role: "menu" },
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  openShellSettings();
                },
              }, gearIcon(16), h("span", null, t("settingsItem"))),
              findSkinSwitcherBtn()
                ? h("button", {
                    className: "al-menu-item",
                    role: "menuitem",
                    onClick: function () {
                      setMenuOpen(false);
                      openSkinSwitcher();
                    },
                  }, shirtIcon(16), h("span", null, t("skinItem")))
                : null,
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  setPanelOpen(true);
                },
              }, evalIcon(16), h("span", null, t("appsItem"))),
              // ── quality-center 集成（dsh-quality-center 插件注入，2026-09-01）──
              // 在「Agent测评」之后追加「质量中心」菜单项，调用 quality-center 插件暴露的 opener
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  if (window.__QUALITY_CENTER_OPEN__) window.__QUALITY_CENTER_OPEN__();
                },
              }, qualityIcon(16), h("span", null, "质量中心")),
              // ── backlog 独立看板集成（dsh-quality-center 插件注入，2026-09）──
              // 在「质量中心」之后追加「遗留项看板」菜单项，调用插件暴露的独立看板 opener
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  if (window.__QUALITY_CENTER_BACKLOG_OPEN__) window.__QUALITY_CENTER_BACKLOG_OPEN__();
                },
              }, backlogIcon(16), h("span", null, "遗留项看板")),
              // ── code-analysis 集成（dsh-code-analysis-platform 插件注入，2026-09-06）──
              // 在「质量中心」之后追加「代码分析」菜单项，调用 code-analysis 插件暴露的 opener
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  if (window.__CODE_ANALYSIS_OPEN__) window.__CODE_ANALYSIS_OPEN__();
                },
              }, codeAnalysisIcon(16), h("span", null, "代码分析")),
              // ── rag-testing 集成（dsh-rag-testing 插件注入，2026-08-21）──
              // 在「Agent测评」之后追加「RAG 测试」菜单项，调用 rag-testing 插件暴露的 opener
              h("button", {
                className: "al-menu-item",
                role: "menuitem",
                onClick: function () {
                  setMenuOpen(false);
                  if (window.__RAG_TESTING_OPEN__) window.__RAG_TESTING_OPEN__();
                },
              }, ragIcon(16), h("span", null, "RAG 测试")))
          : null,
      );
    }

    // =====================================================================
    // center-column overlay (shell.overlay)
    // =====================================================================

    /** 中间栏（conversation 列）元素：布局包的 centerCol，按类名后缀匹配。 */
    function findCenterCol() {
      return window.document.querySelector("[class*='centerCol']");
    }

    function CenterOverlay(props) {
      var t = props.t;
      var open = usePanelOpen();

      var rectPair = useState(null);
      var rect = rectPair[0];
      var setRect = rectPair[1];

      // 打开期间持续跟踪中间栏的视口矩形（侧边栏折叠动画经 ResizeObserver 覆盖）。
      useEffect(function () {
        if (!open) return undefined;
        var el = findCenterCol();
        if (!el) {
          setRect(null);
          return undefined;
        }
        function update() {
          var r = el.getBoundingClientRect();
          setRect({ top: r.top, left: r.left, width: r.width, height: r.height });
        }
        update();
        var observer = null;
        if (typeof window.ResizeObserver === "function") {
          observer = new window.ResizeObserver(update);
          observer.observe(el);
        }
        window.addEventListener("resize", update);
        return function () {
          if (observer) observer.disconnect();
          window.removeEventListener("resize", update);
        };
      }, [open]);

      useEffect(function () {
        if (!open) return undefined;
        function onKeyDown(event) {
          if (event.key === "Escape") setPanelOpen(false);
        }
        window.document.addEventListener("keydown", onKeyDown);
        return function () {
          window.document.removeEventListener("keydown", onKeyDown);
        };
      }, [open]);

      if (!open) return null;
      // 找不到中间栏时退化为全屏。
      var geometry = rect
        ? { top: rect.top + "px", left: rect.left + "px", width: rect.width + "px", height: rect.height + "px" }
        : { top: 0, left: 0, right: 0, bottom: 0 };
      return h("div", { className: "al-overlay", style: geometry, role: "dialog", "aria-label": t("panelTitle") },
        h("div", { className: "al-overlay-header" },
          h("p", { className: "al-overlay-title" }, t("panelTitle")),
          h("button", {
            className: "al-overlay-close",
            "aria-label": t("close"),
            title: t("close") + " (Esc)",
            onClick: function () { setPanelOpen(false); },
          }, "✕")),
        h("div", { className: "al-overlay-body" },
          h(AuditrailPanel, { t: t })),
      );
    }

    // =====================================================================
    // plugin entry
    // =====================================================================

    /** Register the sidebar Apps dropdown and the fullscreen overlay. */
    function apply(ctx) {
      ctx.effect(function () {
        return ctx.locale.register(NS, { zh: zh, en: en });
      }, "auditrail: dictionaries");
      var styleTag = injectCss();
      ctx.slots.inject("sidebar.footer.action", function () {
        return ctx.slots.register({
          name: "sidebar.footer.action",
          id: "auditrail",
          order: 10,
          locale: NS,
          inject: function () { return { t: ctx.locale.bind(NS) }; },
        }, MenuFooterAction);
      });
      ctx.slots.inject("shell.overlay", function () {
        return ctx.slots.register({
          name: "shell.overlay",
          id: "auditrail",
          order: 10,
          locale: NS,
          inject: function () { return { t: ctx.locale.bind(NS) }; },
        }, CenterOverlay);
      });
      return function () {
        if (styleTag) styleTag.remove();
      };
    }

    exports.NS = NS;
    exports.inject = inject;
    exports.apply = apply;
    return module.exports;
  },
});
