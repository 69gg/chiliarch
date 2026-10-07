import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "CHILIARCH_");
  return {
    plugins: [react()],
    build: { outDir: "../src/chiliarch/static", emptyOutDir: true },
    server: env.CHILIARCH_DEV_API_URL
      ? {
          proxy: {
            "/api": {
              target: env.CHILIARCH_DEV_API_URL,
              changeOrigin: true,
              ws: true,
            },
          },
        }
      : undefined,
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      restoreMocks: true,
    },
  };
});
