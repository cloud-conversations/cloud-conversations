/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          info: "#239ff0",
          success: "#74a93c",
          danger: "#ff0000",
        },
        // The banner's wordmark and cloud.
        cc: {
          navy: "#12294d",
          blue: "#1e5ea3",
        },
        link: "#4564E2",
        grey: {
          dark: "#494949",
          DEFAULT: "#7e7e7e",
          med: "#aaaaaa",
          light: "#e5e5e5",
          lighter: "#f2f2f2",
        },
      },
    },
  },
  plugins: [],
};
