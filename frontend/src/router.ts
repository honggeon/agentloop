import { createRouter, createWebHistory } from "vue-router";
import AgentDetailView from "./views/AgentDetailView.vue";
import AgentListView from "./views/AgentListView.vue";
import AgentRunsView from "./views/AgentRunsView.vue";
import BatchView from "./views/BatchView.vue";
import LiveEvalView from "./views/LiveEvalView.vue";
import PlatformView from "./views/PlatformView.vue";
import RunView from "./views/RunView.vue";
import TrajectoryView from "./views/TrajectoryView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "batch",
      component: BatchView,
      meta: { title: "批量评测总览" }
    },
    {
      path: "/runs/:runId",
      name: "run",
      component: RunView,
      meta: { title: "单次评测" }
    },
    {
      path: "/runs/:runId/trajectory",
      name: "trajectory",
      component: TrajectoryView,
      meta: { title: "轨迹" }
    },
    {
      path: "/live",
      name: "live",
      component: LiveEvalView,
      meta: { title: "运行时评测" }
    },
    {
      path: "/platform",
      name: "platform",
      component: PlatformView,
      meta: { title: "测试平台" }
    },
    {
      path: "/agents",
      name: "agents",
      component: AgentListView,
      meta: { title: "Agent 列表" }
    },
    {
      path: "/agents/:agentId",
      name: "agent-detail",
      component: AgentDetailView,
      meta: { title: "Agent 详情" }
    },
    {
      path: "/agents/:agentId/runs",
      name: "agent-runs",
      component: AgentRunsView,
      meta: { title: "Agent 运行记录" }
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: "/"
    }
  ],
  scrollBehavior() {
    return { top: 0 };
  }
});

router.afterEach((to) => {
  const title = typeof to.meta.title === "string" ? to.meta.title : "Auditrail";
  document.title = `${title} · Auditrail`;
});

export default router;
