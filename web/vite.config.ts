import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// 小剧组 · 导演台
// 开发期把 /api 代理到本地后端（scripts/run_server.py，默认 8000），
// 这样前端可以用同源相对路径调用，避免 CORS 与 token 处理分叉。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5183,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 交给 FastAPI 托管时按静态目录服务；此处仅产物输出
    sourcemap: false,
  },
});
