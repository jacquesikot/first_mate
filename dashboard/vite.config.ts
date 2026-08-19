import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the fm daemon at /ui/ (server.py mounts dashboard/dist there).
// `pnpm dev` proxies API + WS to a locally running daemon.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/status": "http://127.0.0.1:8787",
      "/tasks": "http://127.0.0.1:8787",
      "/questions": "http://127.0.0.1:8787",
      "/memory": "http://127.0.0.1:8787",
      "/config": "http://127.0.0.1:8787",
      "/health": "http://127.0.0.1:8787",
      // The new-task flow needs these; without them `pnpm dev` 404s on
      // the repo picker and on every scoping turn.
      "/scoping": "http://127.0.0.1:8787",
      "/fs": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
});
