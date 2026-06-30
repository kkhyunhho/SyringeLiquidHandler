import path from "path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Dev: forward each cell's API to its /v1 server (same-origin, no CORS).
  // Per-cell bases (cells.ts) map to per-cell ports; "/v1" stays as a default.
  // Run a server (real, or `python -m server [--cell weigh] --fake`) per port.
  server: {
    host: true, // bind 0.0.0.0 so VSCode forwarding / NUC-IP access works
    allowedHosts: true, // accept forwarded/tunnel Host headers
    proxy: {
      // cell1 (dispense): /api/cell1/v1/... → :17054/v1/...
      "/api/cell1": {
        target: "http://localhost:17054",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/cell1/, ""),
      },
      // cell4 (weigh): /api/cell4/v1/... → :17060/v1/...
      "/api/cell4": {
        target: "http://localhost:17060",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/cell4/, ""),
      },
      // default (no base) — handy for a single-cell preview
      "/v1": {
        target: process.env.VITE_API_TARGET || "http://localhost:17054",
        changeOrigin: true,
      },
    },
  },
})
