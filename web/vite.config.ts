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
  // Dev: forward /v1/* to the FastAPI server so the browser calls it
  // same-origin (no CORS). Run the server with `python -m server --fake`
  // (or real). Override the target with VITE_API_TARGET if it's elsewhere.
  server: {
    proxy: {
      "/v1": {
        target: process.env.VITE_API_TARGET || "http://localhost:17047",
        changeOrigin: true,
      },
    },
  },
})
