<template>
    <div class="dashboard-bg">
        <NavBar />

        <!-- 主內容區塊 -->
        <div class="main-content d-flex flex-column align-items-center justify-content-center py-5">
            <div class="dashboard-card shadow-lg rounded-4 px-4 py-5 mt-5 mb-5 mx-auto"
                style="max-width: 900px; background: #232b3a; color: #fff;">
                <h2 class="mb-4 text-center dashboard-title">會員專區</h2>
                <p class="text-center mb-5 dashboard-desc">歡迎回來！這裡是您的 AI 工具專屬空間。</p>

                <!-- 服務卡片 -->
                <div class="row g-4 justify-content-center">
                    <div class="col-12">
                        <div class="card service-card-hero w-100">
                            <div class="d-flex flex-column flex-md-row align-items-center justify-content-between p-3 p-md-4 gap-3">
                                <div class="service-text text-center text-md-start px-2 px-md-0">
                                    <h5 class="mb-1 service-title">AI 語音簡報</h5>
                                    <p class="mb-0 service-desc-muted">上傳 PDF 檔案，由 AI 自動生成語音與簡報，並提供字幕與下載功能。</p>
                                </div>

                                <div class="mt-2 mt-md-0">
                                    <router-link to="/video-abstract" class="btn btn-dark-main btn-lg">開始使用</router-link>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup>
import { useRouter } from 'vue-router'
import NavBar from './NavBar.vue'

const router = useRouter()

const logout = () => {
    localStorage.removeItem('token')
    router.push('/login')
}
</script>

<style scoped>
.dashboard-bg {
    background: #181c24;
    min-height: 100vh;
    padding-top: 72px;
}

.main-content {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
}


.dashboard-card {
    border-radius: 1.2rem;
    background: #232b3a;
    color: #fff;
    box-shadow: 0 8px 32px rgba(58, 141, 222, 0.10);
    margin-top: 48px;
    margin-bottom: 48px;
    /* max-width: 900px; */
    width: 100%;
}

.dashboard-title {
    color: #3a8dde;
    font-weight: bold;
    letter-spacing: 1px;
}

.dashboard-desc {
    color: #b0bed9;
    font-size: 1.1rem;
}

.usage-card {
    border-radius: 1.2rem;
    min-height: 160px;
    background: #232b3a;
    color: #fff;
    box-shadow: 0 2px 12px rgba(30, 129, 176, 0.07);
    border: 1.5px solid #2e3a4d;
    transition: box-shadow 0.2s;
}

.usage-card:hover {
    box-shadow: 0 4px 24px #3a8dde22;
}

.usage-label {
    color: #b0bed9;
    font-size: 1.1rem;
}

.usage-number {
    color: #3a8dde;
    font-size: 2.8rem;
}

.usage-icon {
    font-size: 2.2rem;
    color: #3a8dde;
}

.service-card {
    border-radius: 1.2rem;
    min-height: 200px;
    background: #232b3a;
    color: #fff;
    box-shadow: 0 2px 12px rgba(30, 129, 176, 0.07);
    border: 1.5px solid #2e3a4d;
    transition: box-shadow 0.2s;
}

.service-card:hover {
    box-shadow: 0 4px 24px #a259d922;
}

.service-label {
    color: #b0bed9;
    font-size: 1.1rem;
}

.service-desc {
    color: #b0bed9;
    font-size: 1.05rem;
}


/* Hero service card styling */
.service-card-hero {
    background: linear-gradient(180deg, rgba(35,43,58,0.7), rgba(35,43,58,0.85));
    color: #fff;
    border-radius: 1rem;
    /* white thin border */
    border: 1px solid rgba(255,255,255,0.12);
}

/* icon removed - styles cleaned up */

.service-title {
    color: #fff;
    font-weight: 700;
    letter-spacing: 0.4px;
}

.service-desc-muted {
    margin: 0;
    color: #b6c8dd;
    font-size: 0.95rem;
}

@media (max-width: 575px) {
    .service-icon-circle { width: 52px; height: 52px; }
    .service-title { font-size: 1rem; }
    .service-desc-muted { font-size: 0.9rem; }
}

.btn-dark-main {
    background: linear-gradient(90deg, #232a35 0%, #2f78c2 100%);
    color: #fff;
    border: none;
    font-weight: 700;
    border-radius: 0.7rem;
    box-shadow: none;
    padding: 0.55rem 1.05rem;
    transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
}

.btn-dark-main:hover,
.btn-dark-main:focus {
    /* keep gradient direction (left -> right) but brighten the tones */
    background: linear-gradient(90deg, #2e3b4e 0%, #4aa3ff 100%);
    color: #fff;
    transform: translateY(-3px);
    box-shadow: none;
}

.btn-dark-main:focus-visible {
    outline: 3px solid rgba(58,141,222,0.16);
    outline-offset: 2px;
}

.btn-dark-main:disabled,
.btn-dark-main[disabled] {
    background: #6c7a92;
    cursor: not-allowed;
    box-shadow: none;
    transform: none;
}

@media (max-width: 991px) {
    .dashboard-card {
        padding: 1.2rem;
        width: 98vw;
    }

    .main-content {
        padding-left: 0;
        padding-right: 0;
    }
}
</style>
