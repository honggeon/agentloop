import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  envDir: "..",
  plugins: [vue()],
  server: {
    host: "0.0.0.0",
    port: 5173
  }
});
