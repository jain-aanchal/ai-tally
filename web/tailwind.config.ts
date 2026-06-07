// SPDX-License-Identifier: Apache-2.0
import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0f1115",
        panel: "#161a22",
        edge: "#252b37",
        muted: "#8a93a6",
        accent: "#5e6ad2",
        good: "#4cb782",
        warn: "#f2c94c",
        bad: "#eb5757",
      },
    },
  },
  plugins: [],
};

export default config;
