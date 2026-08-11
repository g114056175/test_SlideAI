const VideoAbstractLab = () => import("./components/VideoAbstractLab.vue");

const routes = [
  {
    path: "/",
    alias: "/video-abstract-lab",
    component: VideoAbstractLab,
  },
  {
    path: "/video-abstract",
    redirect: "/",
  },
  {
    path: "/:pathMatch(.*)*",
    redirect: "/",
  },
];

export default routes;
