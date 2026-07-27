# 淘宝浏览器后端路由

## 固定后端

本 Skill 使用 `AIALRA Shopping Browser` 作为淘宝网页后端

它固定运行微软官方 Playwright MCP，并使用仓库外的独立持久 Chrome 资料

用户直接在可见 Chrome 窗口登录，不导出 Cookie、存储状态、密码或验证码

## 使用条件

- 当前任务已经加载 `aialra-shopping-browser` MCP
- 插件版本通过自身仓库验证和本地 MCP 冒烟测试
- 当前 Runner 节点只要求搜索、详情、评价或截图读取
- 当前主机位于工作流允许列表
- 外部观察和淘宝节点输出都能通过各自 validator

条件不满足时报告 `fallback`

已经开始访问淘宝后出现登录、验证码、限流、授权不足或策略阻止时不能切换后端

## 证据交接

插件先把当前页面整理为它的通用观察 JSON

通用校验器检查来源、时间、允许主机、只读状态、链接和敏感字段

通过后再映射为淘宝搜索或详情 Schema

淘宝 validator 继续检查商品链接、价格、SKU、店铺、评价、成本和证据等级

两层校验都通过才能提交给 Runner

## 已调查方案

| 项目 | 结论 | 原因 |
|---|---|---|
| `AIALRA-0/AIALRA-SHOPPING-BROWSER` | 默认后端 | 官方 Playwright MCP、独立持久资料、无需 Cookie 导出、具备证据校验和本地端到端测试 |
| `microsoft/playwright-mcp` | 浏览器核心 | Apache-2.0 许可证、持续维护、标准 MCP、可见 Chrome 和持久资料支持 |
| `JeremyDong22/taobao_mcp` | 只学习架构 | 没有明确许可证、选择器脆弱、批量搜索和测试不足 |
| `donggeai/xianyu-skills` | 只学习桥接思路 | 没有明确许可证、扩展权限过宽、本地 WebSocket 缺少鉴权 |

## 安全停止

登录、扫码、验证码和双重认证由用户亲自完成

限流和人机检查出现后不自动重试

宿主或工具明确返回 `policy-blocked` 时立即结束当前运行

当前运行不能切换浏览器、脚本、接口或其他来源绕过
