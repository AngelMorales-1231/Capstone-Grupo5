import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: "#1F4E5F",
        accent: "#2E86AB",
      },
    },
  },
  plugins: [],
};
export default config;
