# AgentScope Web UI

## Site branding

Sign in as **admin**, open **My Account**, and use **Site branding (global)**
to edit the browser title or upload a PNG, JPEG, WebP, or ICO logo (up to
512 KB). The same logo appears in browser tabs, the top-left sidebar,
the login page and the appearance preview. The preview is local until
**Save global settings** is clicked.
**Restore defaults** fills in BudgetOntology and the original AgentScope icon;
click Save to publish that reset.

Ordinary users cannot see the editor. `PUT /admin/branding` independently
requires an authenticated admin session, a valid Origin, and a CSRF token.
The display-only `GET /branding` endpoint is public so the login page uses
the same branding. Both URLs are accessed through the frontend's `/api` proxy.
The configuration is stored atomically in Redis without a TTL, alongside
the existing authentication store, and survives application restarts.
Saved changes appear immediately in the editing tab; other active pages
refresh on focus or within one minute. If the server is unreachable, the
browser keeps its last successfully loaded branding, or the bundled defaults
on a fresh page.

After updating backend code, restart the agent service before using the
editor. The Windows launcher intentionally disables hot reload. An anonymous
`GET /branding` (or `/api/branding` through the frontend) must return HTTP 200
with the public configuration. A 401 or 404 usually indicates that an older
backend is still running or the frontend proxy targets a different service.

## Appearance

Open **Appearance** from the login page or the sidebar palette icon. The same
controls are available on **My Account**. Choose Light, Dark, or System mode and
a Blue, Teal, or Violet accent color. The default is Light with Blue accents.

Preferences apply immediately, persist in this browser, and synchronize between
tabs. System mode follows changes to the operating system's appearance. Appearance
preferences are independent of the signed-in account and its agent configuration.
Exported reports retain their own document styles.

Use **Import NROS theme** to import a NROS V3 `.tgz` / `.tar.gz` package or a
flat theme `.json` file. A package is read entirely in the browser; it needs
`package/v3/meta.json` (or `v3/meta.json`) and the JSON files listed in `themes`.
The first imported theme is applied immediately; other themes in the package can
be selected from the imported list. Reimporting the same product-line/code pair
updates it. Up to 20 themes are saved locally, with a 2 MB upload and 8 MB
decompressed limit. Storage failures leave the current appearance unchanged.

The adapter maps NROS colors, surfaces, menu selection, buttons, table headers,
font family/size, corner radius, and panel shadow to AgentScope design tokens.
NROS-specific header layouts, menu structure, and NCell controls are not added.
Imported themes use their original colors; select a built-in palette to restore
the previous Light/Dark/System preference. Deleting the active imported theme
also restores that preference. Imports and selections synchronize across tabs.

Run the appearance and theme-import checks with `pnpm test:appearance`.

## 女娲门户嵌入

门户通过 URL App 打开本页面时，地址上会带一次性 `ticket`。前端只把这张票据
交给后端 `GET /api/sso/nvwa`，不在浏览器里保存票据，也不直接请求女娲。
验票和会话由 agent service 完成，配置见
`examples/agent_service/README.md` 的「女娲门户单点登录」。

从菜单重新打开会带一张新票据。同一张已经提交过的票据不会再次发送。验票成功后
刷新页面使用 AgentScope 自己的登录会话。

## 智能体管理

侧栏的「智能体管理」打开 `/agents`，可以新建、搜索、配置、复制和删除智能体。
管理员可按所属用户筛选并管理所有智能体；普通用户只能管理自己的智能体。
配置页包含基础信息、AI 资源、Skill、MCP、知识库、记忆、行为权限及工作空间。
编辑资源会提示受影响的智能体，也可以先建立资源的独立副本。

新会话继承智能体的默认模型、知识库与权限；已有会话保留当前配置。聊天页的
「会话配置来源」可逐项恢复继承，也可取消继承保留当前值。旧智能体可以从已有
会话导入默认配置。没有配置主模型时，需要先选定模型才能发起对话。

复制向导提供分类和逐项选择，Skill、MCP、AI 资源可沿用原资源或创建独立副本。
跨用户复制会创建目标用户的独立资源配置，并要求重新关联知识库。记忆和业务文件
默认不复制；勾选后写入独立工作空间。历史会话、运行状态、定时任务和渠道不会复制。

## React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
	globalIgnores(['dist']),
	{
		files: ['**/*.{ts,tsx}'],
		extends: [
			// Other configs...

			// Remove tseslint.configs.recommended and replace with this
			tseslint.configs.recommendedTypeChecked,
			// Alternatively, use this for stricter rules
			tseslint.configs.strictTypeChecked,
			// Optionally, add this for stylistic rules
			tseslint.configs.stylisticTypeChecked,

			// Other configs...
		],
		languageOptions: {
			parserOptions: {
				project: ['./tsconfig.node.json', './tsconfig.app.json'],
				tsconfigRootDir: import.meta.dirname,
			},
			// other options...
		},
	},
]);
```

You can also install [eslint-plugin-react-x](https://www.npmjs.com/package/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://www.npmjs.com/package/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x';
import reactDom from 'eslint-plugin-react-dom';

export default defineConfig([
	globalIgnores(['dist']),
	{
		files: ['**/*.{ts,tsx}'],
		extends: [
			// Other configs...
			// Enable lint rules for React
			reactX.configs['recommended-typescript'],
			// Enable lint rules for React DOM
			reactDom.configs.recommended,
		],
		languageOptions: {
			parserOptions: {
				project: ['./tsconfig.node.json', './tsconfig.app.json'],
				tsconfigRootDir: import.meta.dirname,
			},
			// other options...
		},
	},
]);
```
