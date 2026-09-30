# Cloudflare Pages 发布

网站是纯静态产物。CPU 图片预处理和去重在 GitHub Actions 完成，Cloudflare 仅托管构建后的文件。使用 [Pages Direct Upload CI](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/) 流程，不启用第二套 Git 自动构建。

图库原图合计上限为 1 GiB，单图大小、像素和动画帧限制独立保留。OCR 使用最多两个并行进程，每个进程限定一个 OpenMP 线程、每张超时 30 秒；超时只标记该图的 OCR 建议失败，不覆盖人工文字。目录检查 job 最多运行 40 分钟，适配数百张的批量投稿。

## 首次配置

1. 在 Cloudflare 中准备名为 `deepseek-chan` 的 Pages 项目，生产分支为 `main`。
2. 在 GitHub Settings → Secrets and variables → Actions 中添加变量 `CLOUDFLARE_ACCOUNT_ID`。
3. 添加 secret `CLOUDFLARE_API_TOKEN`，使用有目标账户 Cloudflare Pages 编辑权限的 API Token。不要把个人 Wrangler OAuth 登录令牌当作长期 CI secret。
4. 在 Pages 的 Custom domains 中添加 `deepseek-chan.ry.rs`，为 `deepseek-chan` 配置指向 `deepseek-chan.pages.dev` 的 CNAME，并等待域名和 HTTPS 证书激活。
5. 运行主分支 `Catalog CI`，验证 `Publish to Cloudflare Pages` 成功，再检查自定义域名。

本项目已准备对应的 Pages 项目和发布配置，但 API Token 属于账户凭据，不能由代码仓库自带。账户变量缺失时部署 job 跳过；账户变量已设置而 token 缺失时部署给出明确失败信息。CI 通过不等于域名已经激活。

## 工作流边界

- PR 和 merge queue 使用目标分支的可信处理程序读取待合并目录，只读取投稿数据，不执行投稿中的入库脚本。
- 独立 `Code tests` job 用于测试程序变更，不接触部署凭据。全部检查只授予只读仓库权限，checkout 不保留凭据。
- PR 的静态预览使用目标分支前端，便于安全审核投稿；前端代码变更需结合源码检查和本地预览审核，合并后构建才使用更新后的前端。
- 只有 `main` 的成功构建或手动主分支运行能够进入生产发布；部署 job 下载同次运行生成的产物，不重新接收外部 PR 产物。
- `production` environment 建议限制仅 `main`。仓库规则建议要求 `Catalog validation` 和 `Code tests`，维护者内容审核，并禁止直接推送绕过 PR。
- 不使用 `pull_request_target` 执行外部代码，不自动写回贡献者分支，不为外部 PR 自动发布公网预览。

生产发布使用锁文件中固定版本的 Wrangler。新版本 CLI 对首次项目创建的行为可能变化，已有项目的日常发布不需要重新创建项目。

## 验证与回退

发布后检查首页、搜索“干饭”、详情、原图下载和手机布局；确认条目计数与当前主分支一致。若发布失败，上一版静态站仍可使用，先阅读 Actions 日志，不要跳过目录校验强行发布。

内容回退通过 revert 或修复 PR 完成。删除条目必须重新构建发布；Git 历史中的原始文件不会因站点更新而消失。
