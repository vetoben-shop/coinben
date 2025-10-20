// eslint.config.mjs
import js from "@eslint/js";
import globals from "globals";

export default [
  // ==== 기본 환경 설정 (Airbnb 스타일 기반, ESM, 브라우저 + Node 병행) ====
  {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      ...js.configs.recommended.rules,
    },
  },

  // ==== server/**/*.js 파일용 Node/CommonJS 오버라이드 ====
  {
    files: ["server/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "commonjs", // require/module 지원
      globals: {
        ...globals.node,
      },
    },
    rules: {
      "no-undef": "off", // require, module, process 등 Node 전역 허용
    },
  },
];
