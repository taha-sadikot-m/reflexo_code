import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "var(--paper)",
        surface: "var(--surface)",
        ink: "var(--ink)",
        brand: "var(--brand)",
        accent: "var(--accent)",
        control: "var(--control)",
        focus: "var(--focus)",
        background: "var(--background)",
        primary: "var(--primary)",
        input: "var(--input)",
        ring: "var(--ring)",
      },
      fontFamily: {
        display: ["var(--font-display)"],
        ui: ["var(--font-ui)"],
        mono: ["var(--font-mono)"],
      },
    },
  },
  plugins: [],
};

export default config;
