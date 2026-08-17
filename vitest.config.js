import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      include: ["static/utils.js"],
      reporter: ["text"],
    },
  },
});