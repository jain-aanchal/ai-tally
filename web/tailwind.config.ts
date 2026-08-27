// SPDX-License-Identifier: Apache-2.0
import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Nord light palette (chosen over the original dark theme): a calm, low-glare snow-storm
        // background with a frost-blue accent and aurora status colors, all kept dark enough to
        // read as text on the light surface. `fg` is the primary text token that replaced the
        // former white/light-gray text the dark theme relied on.
        ink: "#eceff4", // page background (nord6)
        panel: "#ffffff", // cards / raised surfaces
        edge: "#d8dee9", // hairline borders (nord4)
        muted: "#4c566a", // secondary text (nord3), readable on light
        fg: "#2e3440", // primary text (nord0)
        accent: "#5e81ac", // frost blue (nord10)
        good: "#4c9f70", // success, darkened from nord14 so it reads as text
        warn: "#b7791f", // warning, darkened from nord13 so it reads as text
        bad: "#bf616a", // danger (nord11)
      },
    },
  },
  plugins: [],
};

export default config;
