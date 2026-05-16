import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Shared dev config lives in web/.env (one directory up from this file).
const envDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, envDir, "");
  const backendPort = env.BACKEND_PORT || "8765";
  const frontendPort = Number(env.FRONTEND_PORT || 5173);

  return {
    plugins: [react()],
    envDir,
    server: {
      host: "127.0.0.1",
      port: frontendPort,
      strictPort: true,
      proxy: {
        "/api": {
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
  };
});
