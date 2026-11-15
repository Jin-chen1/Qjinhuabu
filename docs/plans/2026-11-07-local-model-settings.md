# 本机模型状态修复计划

**目标：** 在本机豆包工作模式中按真实连接和授权显示模型状态，消除未配置 NewAPI 导致的全模型失败误报。

**设计：** 复用 `/api/v1/doubao-work/status` 的 `reachable`、`connected` 字段和既有 `doubao-status` 查询键。仅本机 CE 且 `VITE_DOUBAO_WORK_ONLY=true` 时启用，查询失败不能沿用缓存显示已连接。展示豆包工作状态；按后续截图要求隐藏其他网关入口，媒体页展示原生上传与本机存储说明。音频、Embedding、翻译和本机工具执行仍需原渠道。

**技术栈：** React、TanStack Query、现有 i18n、Vitest。

- [x] 在设置页和 Header 既有测试中复现本机已连接仍报网关缺失的问题；覆盖未授权、服务断开及普通 CE 网关提醒。
- [x] 新增 `frontend/src/lib/queries/doubao-work.ts`，使用 `queryKey: ["doubao-status"]`、`api.get("api/v1/doubao-work/status", { signal, timeout: 10_000 })`、`retry: false`、15秒刷新；`ready = active && !isError && reachable && connected`。
- [x] 新增本机设置状态组件；SettingsDialog 根据本机模式显示此组件，导航状态使用真实连接状态。Header 在此模式使用同一状态，其他部署保留网关判断。
- [x] 原生工作台复用查询，确保刷新失败不显示缓存“已连接”。补齐四语言文案。
- [x] 运行 `pnpm test` 限定设置、Header、连接状态与模型网关测试，运行 `pnpm build:ce`。通过浏览器验证本机提示与关闭，更新本机使用文档和恢复记忆。

后续截图修复：

- [x] 本机模式移除可选网关 disclosure，其他部署保留原模型配置。
- [x] 媒体存储页使用本机存储说明，不以第三方图床配置判断原生上传是否可用；隐藏本机模式的云存储警告、导航未配置图标和密钥表单。服务断开时仍保持正确存储说明，模型页继续报告断开。
- [x] 回归覆盖本机连接／断开时的存储页面及其他 CE 的真实云存储警告与表单。浏览器在独立测试画布实际打开模型与存储页面，确认其他网关入口、云存储警告及表单均不存在，无横向溢出；未修改用户画布或提交生成任务。

验收：80项相关测试通过（设置54、Header18、连接5、存储凭据3），TypeScript及CE构建通过。浏览器显示“已连接并授权”和“本机媒体存储”。构建仍有原有大包提示，测试框架仍有现有 act 警告。

修改范围限状态展示及其查询，不创建 NewAPI，不修改密钥、生成接线、用户画布或媒体任务。
