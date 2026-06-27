import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served by FastAPI as static assets under /static/spa/. The Python
// package ships the *built* output, so an operator never needs Node on the box.
// `base` makes every asset URL resolve under the mounted static dir; `outDir`
// (relative to this project root) drops the build straight into the package.
export default defineConfig({
  plugins: [react()],
  base: "/static/spa/",
  build: {
    outDir: "../src/pgbench_webapp/static/spa",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    // `npm run dev` proxies the API to a locally-running pgbench-web (TLS, self-signed).
    proxy: {
      "/api": { target: "https://127.0.0.1:8443", changeOrigin: true, secure: false },
      "/runs": { target: "https://127.0.0.1:8443", changeOrigin: true, secure: false },
      "/login": { target: "https://127.0.0.1:8443", changeOrigin: true, secure: false },
      "/logout": { target: "https://127.0.0.1:8443", changeOrigin: true, secure: false },
    },
  },
});
