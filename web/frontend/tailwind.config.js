/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          green: "#6F8E3F",
          red: "#C0202F",
          cream: "#F5E5C4",
        },
      },
    },
  },
  plugins: [],
};
