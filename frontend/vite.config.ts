import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "react-router-dom": fileURLToPath(new URL("./src/lib/router.tsx", import.meta.url)),
      "@tanstack/react-query": fileURLToPath(new URL("./src/lib/reactQuery.tsx", import.meta.url)),
      "framer-motion": fileURLToPath(new URL("./src/lib/motion.tsx", import.meta.url)),
    },
  },
  server: {
    port: 5173,
  },
});
