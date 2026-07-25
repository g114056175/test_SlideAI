import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const devApiTarget = process.env.VITE_API_BASE_URL || process.env.VITE_API_URL || process.env.MOCK_API_URL;

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],

  // 修改 1: 將 base 固定為 "/"
  // 原本的 "/SlideAI/" 通常是給 GitHub Pages 用的。
  // 在 Zeabur 上，您的網站是部署在網域根目錄，設為子路徑會導致 404 錯誤。
  base: "/",

  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["vue", "vue-router"],
          ui: ["vuetify", "bootstrap"],
        },
      },
    },
  },

  // 這是給 "npm run dev" (本地開發) 用的設定
  server: {
    host: "0.0.0.0",
    port: 5174,      // Lab 測試預設 5174
    proxy: devApiTarget ? {
      "/api": {
        target: devApiTarget,
        changeOrigin: true,
      },
    } : undefined,
  },

  // 修改 2: 新增 "preview" 區塊 (關鍵！這是給 Zeabur Docker 部署用的)
  preview: {
    // 讓伺服器監聽 0.0.0.0，這樣 Docker 容器外部才能連線
    host: "0.0.0.0",
    
    // 指定埠號為 8080，這必須對應您 Dockerfile 裡的 EXPOSE 8080
    port: 8080,
    
    // 修改 3: 解決 "Blocked request" 錯誤
    // 設為 true 代表允許任何網域連線 (包含 slideai-frontend.zeabur.app)
    allowedHosts: true,
  },

  optimizeDeps: {
    include: ["vue", "vue-router", "vuetify"],
  },
  define: {
    __VUE_PROD_DEVTOOLS__: false,
  },
});
