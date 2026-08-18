/**
 * dsh-agentloop — client bundle.
 *
 * 在 DSH「设置」中新增一个「Agent 评测」页面：列出本机 DSH 录制的会话
 * （~/.dsh/sessions/<workspace>/<session>/session.jsonl.zstd），一键导入到
 * agentloop 转换为 run 并做确定性审计，再跳转到 agentloop 看板查看完整报告。
 *
 * 数据来源：agentloop FastAPI 后端（默认 http://127.0.0.1:18080），
 * 接口 GET /api/dsh/sessions、POST /api/dsh/import、GET /api/health。
 *
 * Bundle format: `window.__ModuleLoader__.load({ id, factory })` — the exact
 * shape the client-modules host half serves at `/plugins/<id>/client.js`.
 */
window.__ModuleLoader__.load({
  id: "dsh-agentloop",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    var React = require("react");
    var h = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useCallback = React.useCallback;

    // =====================================================================
    // constants
    // =====================================================================

    /** Dictionary namespace owned by this plugin. */
    var NS = "settings.agentloop";

    /** Services required by the Settings registration. */
    var inject = ["slots", "locale"];

    /** localStorage key for the connection config. */
    var LS_KEY = "dsh-agentloop:v1";

    var DEFAULT_CONFIG = {
      apiBase: "http://127.0.0.1:18080",
      dashboardBase: "http://127.0.0.1:6173",
    };

    var zh = {
      nav: "Agent 评测",
      title: "Agentloop · DSH Agent 评测",
      desc: "把本机 DeepSeek Harness 录制的会话导入 agentloop，做确定性工具审计与轨迹分析。需要先启动 agentloop 后端（agentloop 仓库下 ./start.sh）。",
      apiBase: "后端地址",
      dashboardBase: "看板地址",
      save: "保存",
      testConnection: "测试连接",
      statusOk: "已连接",
      statusFail: "连接失败",
      statusUnknown: "未测试",
      refresh: "刷新会话列表",
      loading: "加载中…",
      empty: "未发现 DSH 会话录制（~/.dsh/sessions）。",
      colTitle: "会话",
      colWorkspace: "工作区",
      colMessages: "消息",
      colTools: "工具调用",
      colUpdated: "更新时间",
      colAction: "操作",
      importRun: "导入并审计",
      importing: "导入中…",
      imported: "已导入",
      openDashboard: "打开看板",
      openTrajectory: "查看轨迹",
      resultTitle: "最近导入",
      untitled: "(无标题)",
    };

    var en = {
      nav: "Agent Eval",
      title: "Agentloop · DSH Agent Evaluation",
      desc: "Import locally recorded DeepSeek Harness sessions into agentloop for deterministic tool auditing. Requires the agentloop backend (./start.sh in the agentloop repo).",
      apiBase: "Backend URL",
      dashboardBase: "Dashboard URL",
      save: "Save",
      testConnection: "Test connection",
      statusOk: "Connected",
      statusFail: "Connection failed",
      statusUnknown: "Not tested",
      refresh: "Refresh sessions",
      loading: "Loading…",
      empty: "No DSH session recordings found (~/.dsh/sessions).",
      colTitle: "Session",
      colWorkspace: "Workspace",
      colMessages: "Messages",
      colTools: "Tool calls",
      colUpdated: "Updated",
      colAction: "Action",
      importRun: "Import & audit",
      importing: "Importing…",
      imported: "Imported",
      openDashboard: "Open dashboard",
      openTrajectory: "View trajectory",
      resultTitle: "Recently imported",
      untitled: "(untitled)",
    };

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

    function trimSlash(url) {
      return String(url || "").replace(/\/+$/, "");
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

    function injectCss() {
      var css = [
        ".al-wrap{padding:4px 0;display:flex;flex-direction:column;gap:16px;max-width:960px}",
        ".al-desc{opacity:.75;font-size:13px;line-height:1.6;margin:0}",
        ".al-card{border:1px solid var(--color-border,rgba(127,127,127,.25));border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:10px}",
        ".al-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}",
        ".al-row label{font-size:12px;opacity:.7;min-width:60px}",
        ".al-row input{flex:1;min-width:220px;padding:5px 8px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px}",
        ".al-btn{padding:5px 12px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px;cursor:pointer}",
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
        ".al-title{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".al-ws{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.7}",
        ".al-link{color:var(--color-primary,#4f7cff);text-decoration:none;font-size:12px;margin-right:10px}",
        ".al-muted{opacity:.6;font-size:12px}",
        ".al-section-title{font-size:13px;font-weight:600;margin:0}",
      ].join("\n");
      var tag = window.document.createElement("style");
      tag.textContent = css;
      tag.setAttribute("data-dsh-agentloop", "1");
      window.document.head.appendChild(tag);
      return tag;
    }

    // =====================================================================
    // component
    // =====================================================================

    function AgentLoopSection(props) {
      var t = (props && props.t) || function (key) { return (zh[key] || key); };

      var cfgState = useState(loadConfig);
      var cfg = cfgState[0];
      var setCfg = cfgState[1];

      var healthState = useState("unknown");
      var health = healthState[0];
      var setHealth = healthState[1];

      var sessionsState = useState(null);
      var sessions = sessionsState[0];
      var setSessions = sessionsState[1];

      var loadStateState = useState("");
      var loadState = loadStateState[0];
      var setLoadState = loadStateState[1];

      var errState = useState("");
      var error = errState[0];
      var setError = errState[1];

      var importingState = useState("");
      var importing = importingState[0];
      var setImporting = importingState[1];

      var resultsState = useState([]);
      var results = resultsState[0];
      var setResults = resultsState[1];

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

      var loadSessions = useCallback(function () {
        setLoadState("loading");
        setError("");
        return fetchJSON(cfg.apiBase, "/api/dsh/sessions?limit=50").then(function (body) {
          setSessions((body && body.sessions) || []);
          setLoadState("done");
        }).catch(function (err) {
          setSessions([]);
          setLoadState("done");
          setError(String(err && err.message || err));
        });
      }, [cfg.apiBase]);

      useEffect(function () {
        var cancelled = false;
        fetchJSON(cfg.apiBase, "/api/health").then(function (body) {
          var ok = body && body.status === "ok";
          if (cancelled) return;
          setHealth(ok ? "ok" : "fail");
          if (ok) loadSessions();
        }).catch(function () {
          if (!cancelled) setHealth("fail");
        });
        return function () { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);

      function onImport(item) {
        setImporting(item.session_id);
        setError("");
        fetchJSON(cfg.apiBase, "/api/dsh/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: item.session_id, workspace: item.workspace_dir }),
        }).then(function (body) {
          setResults(function (prev) {
            return [{
              session_id: item.session_id,
              title: item.title,
              run_id: body.run_id,
            }].concat(prev).slice(0, 10);
          });
        }).catch(function (err) {
          setError(String(err && err.message || err));
        }).then(function () {
          setImporting("");
        });
      }

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
            h("button", {
              className: "al-btn",
              onClick: function () { saveConfig(cfg); },
            }, t("save")),
            h("button", {
              className: "al-btn",
              onClick: function () { saveConfig(cfg); checkHealth().then(function (ok) { if (ok) loadSessions(); }); },
            }, t("testConnection")),
            renderHealth(),
          ),
        );
      }

      function renderResults() {
        if (!results.length) return null;
        return h("div", { className: "al-card" },
          h("p", { className: "al-section-title" }, t("resultTitle")),
          results.map(function (item) {
            var runUrl = trimSlash(cfg.dashboardBase) + "/runs/" + encodeURIComponent(item.run_id);
            return h("div", { className: "al-row", key: item.run_id },
              h("span", { className: "al-title" }, item.title || item.session_id),
              h("span", { className: "al-muted" }, item.run_id),
              h("a", { className: "al-link", href: runUrl, target: "_blank", rel: "noreferrer" }, t("openDashboard")),
              h("a", { className: "al-link", href: runUrl + "/trajectory", target: "_blank", rel: "noreferrer" }, t("openTrajectory")),
            );
          }),
        );
      }

      function renderSessions() {
        if (loadState === "loading") return h("p", { className: "al-muted" }, t("loading"));
        if (!sessions) return null;
        if (!sessions.length) return h("p", { className: "al-muted" }, t("empty"));
        return h("table", { className: "al-table" },
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
        );
      }

      return h("div", { className: "al-wrap" },
        h("p", { className: "al-desc" }, t("desc")),
        renderConfig(),
        error ? h("div", { className: "al-error" }, error) : null,
        renderResults(),
        h("div", { className: "al-row" },
          h("p", { className: "al-section-title", style: { flex: 1 } }, t("title")),
          h("button", {
            className: "al-btn",
            onClick: loadSessions,
            disabled: loadState === "loading",
          }, t("refresh")),
        ),
        renderSessions(),
      );
    }

    // =====================================================================
    // plugin entry
    // =====================================================================

    /** Contribute the Agent 评测 page to the settings panel. */
    function apply(ctx) {
      ctx.effect(function () {
        return ctx.locale.register(NS, { zh: zh, en: en });
      }, "agentloop: dictionaries");
      var t = ctx.locale.bind(NS);
      var styleTag = injectCss();
      ctx.slots.inject("settings.section", function () {
        return ctx.slots.register({
          name: "settings.section",
          id: "agentloop",
          order: 40,
          label: function () { return t("nav"); },
          locale: NS,
          inject: function () { return { t: ctx.locale.bind(NS) }; },
        }, AgentLoopSection);
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
