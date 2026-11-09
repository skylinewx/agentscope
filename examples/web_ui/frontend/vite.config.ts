import path from 'path';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';
import svgr from 'vite-plugin-svgr';

export default defineConfig(({ mode }) => {
	const env = loadEnv(mode, process.cwd(), '');
	const agentscopeEnv = loadEnv(mode, process.cwd(), 'AGENTSCOPE_');
	const base = '/' + (env.VITE_PUBLIC_BASE_PATH || '/').replace(/^\/+|\/+$/g, '') + '/';
	const publicBase = base === '//' ? '/' : base;
	const apiPrefix = `${publicBase}api`;
	const proxy = {
		[apiPrefix]: {
			target:
				process.env.AGENTSCOPE_BACKEND_URL ||
				agentscopeEnv.AGENTSCOPE_BACKEND_URL ||
				'http://127.0.0.1:8000',
			// Preserve the public Host so Origin/CSRF checks work through the proxy.
			changeOrigin: false,
			rewrite: (url: string) => url.slice(apiPrefix.length) || '/',
		},
	};
	return {
		base: publicBase,
		plugins: [react(), tailwindcss(), svgr()],
		server: {
			host: true,
			proxy,
		},
		preview: { proxy },
		resolve: {
			alias: {
				'@': path.resolve(__dirname, './src'),
				'next/navigation': path.resolve(__dirname, './src/lib/next-navigation-shim.ts'),
			},
		},
		optimizeDeps: {
			include: ['mime-types'],
		},
	};
});
