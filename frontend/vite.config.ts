import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vue from "@vitejs/plugin-vue";
import { defineConfig, loadEnv, type Plugin } from "vite";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/**
 * 启动后把看板实际监听的地址上报给后端（POST /api/dashboard/register），
 * 让 DSH 插件等入口能精确找到看板端口，不依赖猜测。
 */
function dashboardRegister(apiTarget: string): Plugin {
  return {
    name: "agentloop-dashboard-register",
    configureServer(server) {
      const http = server.httpServer;
      if (!http) return;
      http.once("listening", () => {
        const addr = http.address();
        const port = typeof addr === "object" && addr ? addr.port : server.config.server.port;
        if (!port) return;
        const url = `http://127.0.0.1:${port}`;
        let attempts = 0;
        const send = () => {
          attempts += 1;
          fetch(`${apiTarget}/api/dashboard/register`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url })
          })
            .then((res) => {
              if (!res.ok && attempts < 5) setTimeout(send, 3000);
            })
            .catch(() => {
              if (attempts < 5) setTimeout(send, 3000);
            });
        };
        send();
      });
    }
  };
}

/**
 * dev server 内置的一键启动接口：前端检测到后端 /api/health 不可达时，
 * 提示用户点击按钮，由这里执行仓库根目录的 ./start.sh 拉起后端服务。
 * 仅存在于 Vite dev server，生产构建不包含此能力。
 */
function dashboardLauncher(): Plugin {
  return {
    name: "agentloop-dashboard-launcher",
    configureServer(server) {
      server.middlewares.use("/__launcher/start", (req, res) => {
        res.setHeader("Content-Type", "application/json; charset=utf-8");
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ ok: false, error: "Method Not Allowed" }));
          return;
        }
        execFile("./start.sh", ["start"], { cwd: repoRoot, timeout: 180000 }, (error, stdout, stderr) => {
          const output = `${stdout || ""}${stderr || ""}`.trim().slice(-2000);
          if (error) {
            res.end(JSON.stringify({ ok: false, output, error: error.message }));
            return;
          }
          res.end(JSON.stringify({ ok: true, output }));
        });
      });
    }
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "");
  const backendPort = env.BACKEND_PORT || "18080";
  const apiTarget = (env.VITE_API_TARGET || `http://localhost:${backendPort}`).replace(/\/$/, "");
  return {
    envDir: "..",
    plugins: [vue(), dashboardRegister(apiTarget), dashboardLauncher()],
    server: {
      host: "0.0.0.0",
      port: Number(env.FRONTEND_PORT || 5111),
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true
        }
      }
    }
  };
});
