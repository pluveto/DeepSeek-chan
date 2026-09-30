# DeepSeek-chan · 蓝色大肥鱼表情墙

一个由社区共同维护的蓝色大肥鱼聊天表情墙。非 DeepSeek 官方项目，与 DeepSeek 无隶属关系。

目标站点：[deepseek-chan.ry.rs](https://deepseek-chan.ry.rs) · [贡献指南](CONTRIBUTING.md) · [内容规则](docs/content-policy.md)

**一个 PR 可以包含多个 mutation，也可以同时新增、修改、替换、调整预处理、删除和合并多个表情。** 仓库中的原图与描述文件是唯一数据源；GitHub Actions 从整个 PR 的最终状态识别全部变更、检查重复、生成展示资源。贡献者无需安装工具或手动运行脚本。

## 浏览与搜索

- 响应式表情墙，按图片文字、标题和标签搜索；无需在线后端、数据库或 AI 服务。
- 查看、下载和复制处理后的表情，另有投稿原图入口；可以查看来源和跳转 GitHub 编辑。
- CI 自动处理截图的连续非白色纯色边缘，支持用描述文件指定裁剪范围和缩小尺寸，绝不放大图片。投稿原图保持不变；动画默认原样保留。
- 首批 25 张为从 [蓝色大肥鱼档案馆](https://github.com/EDMOK/blue-fish-archive) 逐张筛选的聊天反应表情，每张记录固定提交的来源地址。

“干饭”靠图片文字和标签匹配，当前没有语义搜索。我们只收纳适合日常聊天的表情，不收纳涩图、以身体展示为主的插画或与表情用途无关的图片。

## 无需脚本的贡献流程

1. 在 GitHub 网页 fork 仓库，为表情添加 `stickers/<稳定ID>/original.png` 和 `metadata.yaml`，或修改、删除已有条目。
2. 在同一分支完成任意数量的变更，提交一个 PR。
3. Actions 校验完整目录，生成 mutation 清单、图片预处理报告、重复检查、OCR 建议和可下载静态预览。
4. 维护者对照投稿原图和处理结果审核内容、出处与疑似重复；通过后合并，主分支自动重新构建并发布。

在 Actions 下载 `catalog-review` 报告并解压，双击 `review.html` 即可离线查看处理前后图、图片文字、出处及裁剪说明，不需要运行脚本或启动服务。

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。同底图不同配字默认保留；完全相同文件阻止合并。近似图片以 pHash 和文字编辑距离提供提示，最终由人判断，不使用 AI 自动删图。

## 项目结构

```text
stickers/<id>/      原图和手工维护的 metadata.yaml
redirects.yaml     被合并 ID 到保留 ID 的映射
scripts/           面向对象的入库与构建工具
web/               静态前端
tests/             目录、去重与 mutation 行为测试
.github/           PR 检查、内容审核入口与发布流水线
```

处理后的图片、缩略图、哈希、搜索索引和报告均为构建产物，不回写贡献者分支，也不提交到源码仓库。前端与处理逻辑遵循面向对象设计，以领域对象和有明确职责的模块组织；不为静态站引入应用服务器。

## 部署与开发

GitHub Actions 使用 Cloudflare Pages Direct Upload 发布。生产项目名为 `deepseek-chan`，域名为 `deepseek-chan.ry.rs`。仓库需要配置 `CLOUDFLARE_ACCOUNT_ID` 变量和 `CLOUDFLARE_API_TOKEN` secret；缺少凭据不能完成自动发布。域名还需在 Pages 中绑定并等待 DNS/TLS 生效。[部署说明](docs/deployment.md) 包含配置和验证步骤。

仅修改程序的开发者需要本地环境：Python 3.12、Node.js 22。安装 `requirements.txt` 与 `npm ci` 的依赖后，可运行 `npm test`、`npm run build`，再以 `npm run dev` 预览。普通表情贡献者无需执行这些命令。

## 许可与来源

[MIT](LICENSE) 仅覆盖本项目原创代码。`stickers/` 中的原图及第三方素材不自动适用 MIT，版权归各自权利人所有。来源公开不等于授权明确；首批素材作者及许可无法确认的字段保持空值，请逐图核实使用范围。

感谢 [EDMOK / 蓝色大肥鱼档案馆](https://github.com/EDMOK/blue-fish-archive) 的整理与收录工作。首批图库整理自该档案馆，不是本项目原创，也不将档案馆维护者默认当作每张图片的作者。每张表情都保留原图、水印及指向上游固定提交的出处链接；欢迎补充可核实的原作者与原始发布地址。

纠正出处、申请下架或报告不合适内容，请使用 [内容问题入口](https://github.com/pluveto/DeepSeek-chan/issues/new?template=content.yml)。删除会使图片退出当前站点，但不会抹除 Git 历史、已有 fork 或他人下载。
