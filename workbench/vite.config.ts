import { defineConfig } from "vitest/config";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [tailwindcss()],
  test: {
    globals: true,
    environment: "jsdom",
    include: ["__tests__/**/*.test.ts"],
    coverage: { provider: "v8", reporter: ["text", "lcov"] },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
