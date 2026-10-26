import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  {
    // eslint-config-next registers the jsx-a11y plugin with only a few rules; enable the full
    // recommended set (WCAG 2.2 AA is a requirement of this project).
    files: ["**/*.{ts,tsx}"],
    rules: jsxA11y.flatConfigs.recommended.rules,
  },
  {
    // User-visible text lives in messages/*.json, never in components.
    files: ["src/**/*.tsx"],
    ignores: ["src/**/*.test.tsx"],
    rules: { "react/jsx-no-literals": "error" },
  },
  globalIgnores([
    ".next/**",
    "out/**",
    "next-env.d.ts",
    "playwright-report/**",
    "test-results/**",
    "coverage/**",
  ]),
]);
