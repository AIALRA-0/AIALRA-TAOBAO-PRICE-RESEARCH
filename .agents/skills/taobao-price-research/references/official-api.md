# 淘宝官方接口接入

## 这条通道解决什么问题

当前宿主浏览器不允许访问淘宝页面时，Runner 可以改用淘宝开放平台的只读接口继续发现候选商品

官方接口不需要保存淘宝登录 Cookie，也不会控制用户账号

它需要开发者自己的 `App Key`、`App Secret` 和淘宝客推广位编号

## 它能提供什么

候选发现使用 `taobao.tbk.dg.material.optional.upgrade`

这个接口可以按关键词分页搜索淘宝客物料，并返回商品标题、主图、店铺、销量、销售价、预估到手价和商品链接

详情核验使用 `taobao.tbk.item.details.upgrade.get`

这个接口可以按商品编号返回 SKU、SKU 库存、SKU 销售价、预估到手价、邮费、店铺和商品图片

官方文档：

- [淘宝客物料搜索升级版](https://developer.alibaba.com/docs/api.htm?apiId=64759)
- [淘宝客商品详情升级版](https://developer.alibaba.com/docs/api.htm?apiId=64757)
- [淘宝开放平台调用与签名规则](https://developer.alibaba.com/docs/doc.htm.htm?articleId=101617&docType=1&treeId=1)
- [淘宝开放平台新手指南](https://developer.alibaba.com/docs/doc.htm?articleId=118395&docType=1&source=search&treeId=1)

## 它不能提供什么

淘宝客接口只覆盖允许推广的商品

它不能证明已经搜索淘宝全站

它不提供评价正文、完整退货条件和完整保修信息

部分字段由应用权限等级决定，接口文档列出字段并不表示当前应用一定能够获得该字段

因此官方接口结果会保留覆盖警告

浏览器可用时，详情页仍然负责补充评价、退货、保修和页面条件

## 用户需要准备什么

先登录淘宝开放平台并创建应用

在应用概览中取得 `App Key` 和 `App Secret`

再登录淘宝联盟，在推广位管理中取得推广位编号

推广位编号来自 `mm_xxx_xxx_12345678` 的最后一段数字

把三个值放进运行 Codex 的本机环境变量：

```bash
export TAOBAO_TOP_APP_KEY="你的 App Key"
export TAOBAO_TOP_APP_SECRET="你的 App Secret"
export TAOBAO_TBK_ADZONE_ID="推广位最后一段数字"
```

可选签名算法：

```bash
export TAOBAO_TOP_SIGN_METHOD="hmac-sha256"
```

允许值为 `hmac-sha256`、`hmac` 或 `md5`

默认使用淘宝开放平台文档支持的 `hmac-sha256`

## 凭据放在哪里

凭据只放在本机环境变量或系统密钥存储

不要把真实值写进仓库、输入 JSON、运行产物、日志、测试、学习记录或对话

仓库已经忽略 `.env` 和常见凭据文件

代码只把 `App Secret` 用于本地签名，不把它作为请求参数发送

错误输出不会回显三个凭据

## Runner 怎样选择通道

Runner 先尝试淘宝页面，因为页面覆盖范围更接近消费者实际搜索结果

浏览器被宿主策略禁止、页面不可读或搜索失败时，Runner 进入官方候选接口

官方候选接口也不可用时，Runner 进入人工核验回退

详情页不可读时，Runner 进入官方详情接口

官方详情接口也无法确认目标 SKU 时，Runner 返回有限证据，不宣布未经核验的最低价赢家
