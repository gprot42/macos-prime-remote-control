/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        prime: {
          blue: "#00A8E1",
          dark: "#0F171E",
          card: "#1A242F",
          hover: "#232F3E",
        },
      },
      fontFamily: {
        sans: [
          "Amazon Ember",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
