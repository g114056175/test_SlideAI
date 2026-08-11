import { createApp } from "vue";
import {
  createRouter,
  createWebHistory,
} from "vue-router";
import App from "./App.vue";
import routes from "./routes";
import "./style.css";
import "bootstrap/dist/css/bootstrap.min.css";
// Vuetify
import "vuetify/styles";
import { createVuetify } from "vuetify";
import { aliases, mdi } from "vuetify/iconsets/mdi";
import "@mdi/font/css/materialdesignicons.css";

const vuetify = createVuetify({
  icons: {
    defaultSet: "mdi",
    aliases,
    sets: { mdi },
  },
});

const router = createRouter({
  // 統一使用 history 模式；Docker Nginx 負責 SPA fallback。
  history: createWebHistory(),
  routes,
});

createApp(App).use(router).use(vuetify).mount("#app");
