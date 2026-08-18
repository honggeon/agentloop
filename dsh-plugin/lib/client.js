/**
 * dsh-agentloop — client bundle.
 *
 * 在 DSH「设置」中新增一个「Agent 评测」页面：
 *   1. 列出本机 DSH 录制的会话，一键导入 agentloop 做确定性审计；
 *   2. 直接在页面内展示评测指标——审计概览、发现、工具调用明细、
 *      阶段划分、轨迹预览，以及（可选）DeepAgent 裁判的 13 维评分。
 *
 * 数据来源：agentloop FastAPI 后端（默认 http://127.0.0.1:18080）。
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

    /**
     * 评测维度中文标签。key 必须以 agentloop.agents.schemas.DIMENSION_DEFINITIONS
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
      nav: "Agent 评测",
      title: "Agentloop · DSH Agent 评测",
      desc: "把本机 DeepSeek Harness 录制的会话导入 agentloop，做确定性工具审计与轨迹分析；指标直接在本页展示。需要先启动 agentloop 后端（agentloop 仓库下 ./start.sh）。",
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
      runsTitle: "评测结果（已导入的 DSH 会话）",
      noRuns: "还没有导入的 DSH 会话，先在上方导入一个。",
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
      judgeMissing: "尚未运行裁判。裁判需要模型凭据（在 agentloop 的 .env 中配置）。",
      judgeOverall: "总体结论",
      judgeScore: "完成度得分",
      dimTitle: "评测维度",
      untitled: "(无标题)",
      external: "外部看板",
    };

    var en = {
      nav: "Agent Eval",
      title: "Agentloop · DSH Agent Evaluation",
      desc: "Import locally recorded DeepSeek Harness sessions into agentloop for deterministic tool auditing; metrics render right here. Requires the agentloop backend (./start.sh in the agentloop repo).",
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
      runsTitle: "Evaluation results (imported DSH sessions)",
      noRuns: "No imported DSH sessions yet — import one above.",
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
      judgeMissing: "Judge has not run yet. It needs model credentials (configured in agentloop's .env).",
      judgeOverall: "Overall",
      judgeScore: "Completion score",
      dimTitle: "Dimensions",
      untitled: "(untitled)",
      external: "External dashboard",
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

    function injectCss() {
      var css = [
        ".al-wrap{padding:4px 0;display:flex;flex-direction:column;gap:16px;max-width:1000px}",
        ".al-desc{opacity:.75;font-size:13px;line-height:1.6;margin:0}",
        ".al-card{border:1px solid var(--color-border,rgba(127,127,127,.25));border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:10px}",
        ".al-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}",
        ".al-row label{font-size:12px;opacity:.7;min-width:60px}",
        ".al-row input{flex:1;min-width:200px;padding:5px 8px;border-radius:6px;border:1px solid var(--color-border,rgba(127,127,127,.35));background:transparent;color:inherit;font-size:12px}",
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
        ".al-scroll .al-table td{border-bottom:1px solid var(--color-border,rgba(127,127,127,.08))}",
        ".al-pre{font-family:ui-monospace,monospace;font-size:11px;white-space:pre-wrap;word-break:break-all;opacity:.85;margin:0}",
        ".al-msg{border-bottom:1px solid var(--color-border,rgba(127,127,127,.1));padding:6px 8px;font-size:12px}",
        ".al-msg-role{display:inline-block;min-width:64px;font-weight:600;margin-right:6px}",
        ".al-dim{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--color-border,rgba(127,127,127,.08));font-size:12px}",
        ".al-dim-label{min-width:88px}",
        ".al-dim-bar{flex:1;height:6px;border-radius:3px;background:rgba(127,127,127,.18);overflow:hidden}",
        ".al-dim-bar-fill{height:100%;border-radius:3px}",
        ".al-dim-score{min-width:36px;text-align:right}",
      ].join("\n");
      var tag = window.document.createElement("style");
      tag.textContent = css;
      tag.setAttribute("data-dsh-agentloop", "1");
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
      return h("div", { className: "al-scroll" }, messages.map(function (m, i) {
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
    // main component
    // =====================================================================

    function AgentLoopSection(props) {
      var t = (props && props.t) || function (key) { return (zh[key] || key); };

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

      var loadSessions = useCallback(function () {
        return fetchJSON(cfg.apiBase, "/api/dsh/sessions?limit=50").then(function (body) {
          setSessions((body && body.sessions) || []);
        });
      }, [cfg.apiBase]);

      var loadRuns = useCallback(function () {
        return fetchJSON(cfg.apiBase, "/api/runs").then(function (body) {
          var items = ((body && body.runs) || []).filter(function (item) {
            return item.source_base_url === "dsh://local";
          });
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
          if (ok) refreshAll();
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
              h("th", null, t("colJudge")),
              h("th", null, t("colAction")),
            )),
            h("tbody", null, runs.map(function (item) {
              var runId = String(item.name || "");
              var isSelected = selectedRun === runId;
              return h("tr", { key: runId },
                h("td", null, h("div", { className: "al-title", title: runId }, runId)),
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
        renderRuns(),
        renderDetail(),
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
