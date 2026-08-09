const Register = () => import("./components/Register.vue");
const Login = () => import("./components/Login.vue");
const ForgotPassword = () => import("./components/ForgotPassword.vue");
const ResetPassword = () => import("./components/ResetPassword.vue");
const Dashboard = () => import("./components/Dashboard.vue");
const Landing = () => import("./components/Landing.vue");
const VideoAbstract = () => import("./components/VideoAbstract.vue");
const VideoAbstractLab = () => import("./components/VideoAbstractLab.vue");
const AdminDashboard = () => import("./components/AdminDashboard.vue");

const routes = [
  { path: "/", component: Landing },
  { path: "/register", component: Register },
  { path: "/login", component: Login },
  { path: "/forgot-password", component: ForgotPassword },
  { path: "/reset-password", component: ResetPassword },
  { path: "/dashboard", component: Dashboard, meta: { requiresAuth: true } },
  {
    path: "/video-abstract",
    component: VideoAbstract,
    meta: { requiresAuth: true },
  },
  {
    path: "/video-abstract-lab",
    component: VideoAbstractLab,
  },
  
  {
    path: "/admin",
    component: AdminDashboard,
    meta: { requiresAuth: true, isAdmin: true },
  },
  // TestPage route removed
];

export default routes;
