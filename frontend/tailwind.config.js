/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: "#0d1117",
          secondary: "#161b22",
          tertiary: "#21262d",
          hover: "#30363d",
        },
        border: {
          DEFAULT: "#30363d",
          subtle: "#21262d",
        },
        accent: {
          DEFAULT: "#2f81f7",
          hover: "#1f6feb",
        },
      },
    },
  },
  plugins: [],
};
