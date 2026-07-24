# 淘宝商品查价与风险研究

## 这个仓库能做什么

这个仓库维护 `$taobao-price-research` Skill

用户提出淘宝查价请求后，Skill 会生成多个搜索词、批量收集候选商品、读取入围详情、核验目标规格、分析店铺与评价风险，并找出最低价可行商品

Skill 只执行读取和分析

它不会自动下单、联系卖家、收藏、关注、领券、加购或绕过登录验证

## 一次任务怎样运行

```mermaid
flowchart LR
    A["用户说明商品和约束"] --> B["Agent 整理研究计划"]
    B --> C["浏览器批量读取淘宝搜索结果"]
    C --> D["脚本去重并生成详情候选"]
    D --> E["浏览器核验商品详情与评价"]
    E --> F["脚本复算成本 风险和赢家"]
    F --> G["Runner 检查通过后交付结果"]
```

搜索结果卡片只能发现候选

具体商品页确认目标 SKU 后才能成为 A 级价格证据

最终 validator 会重新核对赢家是否为同组可行商品中的最低可比价格

## 当前采集范围

默认研究预算：

- 生成 2 至 4 个搜索词 —— 降低单一关键词漏查
- 每个搜索词读取 1 至 3 页 —— 控制速度和页面压力
- 最多收集 80 个候选商品 —— 防止无界采集
- 最多核验 20 个详情页 —— 把时间集中在低价入围商品
- 最多深入读取 8 个商品的评价信号 —— 优先核查最终候选

每次实际预算由入口研究计划声明，并且不能超过工作流上限

## 当前实测状态

确定性流程、外部节点提交、登录暂停、去重、详情证据校验、风险排名和最终赢家复算已经通过自动测试

2026-07-24 的真实运行中，当前 Codex 浏览器安全策略分别禁止访问淘宝搜索页和淘宝首页

这次阻止发生在淘宝页面加载和登录之前

用户是否已经登录不会改变这个宿主策略结果

浏览器返回的关键原文是：

```text
Browser Use rejected this action due to browser security policy
Browser use is not permitted on https://www.taobao.com
The agent must not attempt the same outcome through alternate browser surfaces or policy circumvention
```

桌面 Computer Use 读取 Codex 设置界面时也在动作发生前被拒绝：

```text
Computer Use is not allowed to use the app 'com.openai.codex' for safety reasons
```

Runner 现在会直接结束为 `failed`

失败结果不会生成接口替代价格、人工替代结果或最低价结论

## 怎样解除网站阻止

内置浏览器：

1. 打开 Codex 设置
2. 进入 `Browser`
3. 找到网站允许与阻止管理
4. 从阻止列表移除 `taobao.com`
5. 开始一个新的淘宝查价任务
6. Codex 请求访问淘宝时选择本次允许或允许此网站

Chrome 扩展：

1. 安装并启用 Codex 的 Chrome 插件
2. 打开 Codex 设置
3. 进入 `Computer Use`
4. 在 Google Chrome 后选择 `Manage`
5. 从阻止列表移除 `taobao.com`
6. 在一个新任务中明确写 `使用 Chrome 打开淘宝并查价`

网站已经被安全策略拒绝后，不能在同一次任务中切换到另一浏览器绕过拒绝

移除阻止后必须开始一次新任务，再由浏览器正常请求访问许可

## 最终结果包含什么

- 查询时间、收货城市级地区、目标规格、成色和会员假设
- 商品价、运费、已验证优惠和已知总额
- 店铺名称、店铺类型、评分、目标 SKU 和库存
- 宣传图、评价负面主题、退货与保修信息
- 风险分、风险理由、排除理由和商品直达链接
- 最低价可行商品或没有可行商品的明确结论
- 搜索词、读取页数、候选数量、详情核验数量和覆盖缺口

## 怎样使用

先在本机创建 Codex Skill 链接：

```bash
python3 scripts/install_local.py
```

安装器把 `~/.codex/skills/taobao-price-research` 链接到当前仓库中的真实 Skill 目录

符号链接让 Codex 能够发现 Skill，同时保留 Runner 需要的核心锁、版本、学习目录和运行状态目录

安装器不会覆盖已经存在的路径

移动或删除当前仓库会使链接失效

安装完成后，Skill 会在 Codex 的下一轮任务中可用

把自然语言请求转换成入口 JSON

示例：

```json
{
  "request_text": "搜索淘宝上全新的 RTX 5070 Ti 16GB 显卡 排除配件 定金 预售 二手和明显高风险店铺 找出最便宜的可行商品",
  "destination_region": "上海",
  "membership_notes": "无已知会员权益"
}
```

启动 Runner：

```bash
python3 .agents/skills/taobao-price-research/scripts/runner.py start --input input.json
```

按照 Runner 返回的 `next_command` 推进

外部节点成功时使用 `submit`

登录或验证码需要用户操作时使用 `fail --kind user-required`

宿主策略禁止页面访问时使用 `fail --kind policy-blocked`

Runner 会直接结束为 `failed`

只有 `status=completed` 才表示最终结果通过机器检查

完整 Agent 协议位于 [SKILL.md](.agents/skills/taobao-price-research/SKILL.md)

## 维护者怎样验证

依次运行：

```bash
python3 scripts/validate.py --ignore-core-lock
python3 -m unittest discover -s tests -v
python3 scripts/check_secrets.py
python3 .agents/skills/taobao-price-research/scripts/freeze_core.py
python3 scripts/validate.py
python3 .agents/skills/taobao-price-research/scripts/freeze_core.py --check
```

测试覆盖正常完成、去重、排除错误商品、优惠计算、风险排名、虚假赢家拒绝、登录暂停、策略阻止硬停止和本地安装

## 人工审计顺序

1. 阅读本页 —— 先理解目标、边界和完整流程
2. 阅读 `SKILL.md` —— 核对 Agent 必须遵守的运行协议
3. 阅读 `workflow.yaml` —— 核对节点、执行器、预算和失败路径
4. 阅读 `references/browser-collection.md` —— 核对页面采集字段与登录边界
5. 阅读 `references/risk-ranking.md` —— 核对风险分、成本公式和赢家规则
6. 阅读 `schemas/` —— 核对每个阶段允许提交的数据
7. 阅读领域脚本 —— 核对去重、详情校验、成本计算和最终复算
8. 阅读 `tests/test_taobao_domain.py` —— 用具体案例确认规则真的生效
9. 阅读 `SECURITY.md` —— 最后核对凭据、隐私和外部副作用

完成这个顺序后，你能够理解一次淘宝查价怎样从用户请求变成可复核结论
