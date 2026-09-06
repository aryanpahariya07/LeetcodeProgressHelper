import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: [".output", ".wxt", "node_modules"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: {
        chrome: "readonly",
        document: "readonly",
        window: "readonly",
        location: "readonly",
        history: "readonly",
        navigator: "readonly",
        fetch: "readonly",
        crypto: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        queueMicrotask: "readonly",
        MutationObserver: "readonly",
        defineBackground: "readonly",
        defineContentScript: "readonly",
      },
    },
  },
);
