// API 配置文件
// 正式外網由 Cloudflare Worker 接住同網域 /api，再由 Worker 轉發後端。
// 瀏覽器端不可包含 Cloudflare Access service token 或後端私密 origin 憑證。

const stripTrailingSlash = (value) => String(value || "").replace(/\/+$/, "");

const getApiUrl = () => {
  const host = window?.location?.hostname || "";
  const protocol = window?.location?.protocol || "";
  const isLocalHost = host === "localhost" || host === "127.0.0.1";
  const isIpv4 = /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  const isGateHost = host === "awinlab-gate.g114056175.me";

  // The official Cloudflare/Zero Trust entry must always use same-origin /api.
  // This protects external users even if a LAN build accidentally injected
  // VITE_API_BASE_URL=http://192.168.x.x:8002 into the bundle.
  if (isGateHost || (protocol === "https:" && !isLocalHost && !isIpv4)) {
    return "/api";
  }

  const explicitBase = import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL;
  // LAN / local standalone mode: the lightweight SPA server only serves static
  // files and does not proxy /api.  Connect to the colocated FastAPI service
  // directly so persisted runs remain available after a production rebuild.
  if (isLocalHost || isIpv4) {
    return stripTrailingSlash(explicitBase || `${protocol || "http:"}//${host}:8002`);
  }
  return stripTrailingSlash(explicitBase || "/api");
};

export const API_BASE_URL = getApiUrl();

// 簡單的請求快取
const requestCache = new Map();
const CACHE_DURATION = 5 * 60 * 1000; // 5分鐘

// API 端點
export const API_ENDPOINTS = {
  VIDEO_ABSTRACT: "/api/video-abstract",
};

const normalizeEndpoint = (endpoint) => {
  const raw = String(endpoint || "");
  if (!raw) return "";
  return raw.startsWith("/") ? raw : `/${raw}`;
};

// 創建完整的 API URL
export const getApiEndpoint = (endpoint) => {
  if (/^https?:\/\//i.test(String(endpoint || ""))) {
    return String(endpoint);
  }
  const normalized = normalizeEndpoint(endpoint);

  if (API_BASE_URL === "/api") {
    if (normalized === "/api") return "/api";
    return normalized.startsWith("/api/") ? normalized : `/api${normalized}`;
  }

  return `${API_BASE_URL}${normalized}`;
};

// 檢查快取是否有效
const isCacheValid = (cacheEntry) => {
  return cacheEntry && Date.now() - cacheEntry.timestamp < CACHE_DURATION;
};

// 通用的 fetch 函數
export const apiRequest = async (endpoint, options = {}) => {
  const url = getApiEndpoint(endpoint);

  // 對於 GET 請求，檢查快取
  if (options.method === "GET" || !options.method) {
    const cacheKey = url;
    const cachedResponse = requestCache.get(cacheKey);

    if (isCacheValid(cachedResponse)) {
      console.log("使用快取響應:", endpoint);
      return cachedResponse.data;
    }
  }

  const defaultHeaders = {
    "Content-Type": "application/json",
  };

  const config = {
    ...options,
    headers: {
      ...defaultHeaders,
      ...options.headers,
    },
  };

  // 添加超時設定
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000); // 30秒超時

  try {
    const response = await fetch(url, {
      ...config,
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      console.error("API Error Details:", {
        url,
        status: response.status,
        statusText: response.statusText,
        errorData,
        headers: Object.fromEntries(response.headers.entries()),
      });
      throw new Error(
        errorData.detail || `HTTP error! status: ${response.status}`
      );
    }

    const data = await response.json();

    // 對於 GET 請求，存入快取
    if (options.method === "GET" || !options.method) {
      const cacheKey = url;
      requestCache.set(cacheKey, {
        data,
        timestamp: Date.now(),
      });
    }

    return data;
  } catch (error) {
    clearTimeout(timeoutId);

    if (error.name === "AbortError") {
      throw new Error("請求超時，請稍後再試");
    }

    console.error("API request failed:", error);
    throw error;
  }
};

export const apiFetch = apiRequest;

// 清理過期的快取
export const clearExpiredCache = () => {
  const now = Date.now();
  for (const [key, value] of requestCache.entries()) {
    if (now - value.timestamp > CACHE_DURATION) {
      requestCache.delete(key);
    }
  }
};

/**
 * Remove all cache entries whose key starts with the given endpoint path.
 * Call this before re-fetching a resource that was just mutated on the server.
 * Example: clearEndpointCache('/api/video-runs')
 */
export const clearEndpointCache = (endpoint) => {
  const prefix = getApiEndpoint(endpoint)
  for (const key of requestCache.keys()) {
    if (key.startsWith(prefix)) {
      requestCache.delete(key)
    }
  }
};

// 定期清理快取（每10分鐘）
setInterval(clearExpiredCache, 10 * 60 * 1000);
