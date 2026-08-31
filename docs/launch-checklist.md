# 公开日 runbook(用完可删)

一切已备好,公开 = 按此清单执行。前置已完成项:全历史敏感扫描(53 提交,无
.env/密钥/内部名泄漏,2026-08-31 复核)、CI 工作流(ubuntu 3.10–3.13 + Windows
3.10/3.13 + 窄编码冒烟)、公开文档六件套(architecture / accuracy / privacy /
configuration / python-api / known-boundaries)、README.pypi 英文单语版。

## 1. 建仓与推送

- [ ] GitHub 新建 **Public** 仓库(不要自动初始化 README/license)。
      确定 `<org>/<repo>`,例如 `yourname/fineprint`。
- [ ] 推送全历史与标签:

```bash
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin main --tags
```

- [ ] 仓库 About 栏:描述用 pyproject 的 description;topics 建议
      `dbt, data-lineage, metrics, data-quality, sqlglot, llm, analytics-engineering`。
- [ ] Settings → 开启 Issues。

## 2. 替换占位符(一次 sed)

- [ ] README.pypi.md 里 9 处 `<org>`:

```bash
perl -pi -e 's{<org>/fineprint}{<org>/<repo>}g; s{github.com/<org>}{github.com/<org>}g' README.pypi.md
```

  (手动把命令里的 `<org>/<repo>` 换成真实值再执行;执行后 `grep '<org>' README.pypi.md` 应为空。)

- [ ] pyproject.toml 解注 `[project.urls]` 四行并填真实 `<org>/<repo>`。
- [ ] README.pypi.md 的 Example 之后补一行 quickstart 链接:
      `Try it in 10 minutes with the bundled example project: <repo>/tree/main/examples/quickstart`
      (docs/FinePrint_zh.md 同步补中文一行;README.zh.md 顶部已有。)
- [ ] docs/FinePrint_zh.md 末尾补联系方式(issues 链接即可;中文页目前没有任何联系渠道)。

## 3. 发版让 PyPI 页面生效

- [ ] 版本号 → 0.9.4,CHANGELOG 记「repo public + urls + en-only PyPI page」。
- [ ] `bash scripts/release_public.sh` → `twine upload dist/fineprint-0.9.4*` → `git tag v0.9.4 && git push --tags`。
- [ ] 验证 PyPI 页:侧栏出现 Homepage/Repository/Issues/Changelog 链接;
      正文顶部"Source & issues / 中文文档"两个链接可点。

## 4. 公开后一小时内

- [ ] CI 首跑全绿(Actions 页,6 个 job)。Windows 红了优先看编码/路径类失败
      (tests/test_console_encoding.py 是哨兵)。
- [ ] README.md 顶部加 CI badge(可选):
      `![ci](https://github.com/<org>/<repo>/actions/workflows/ci.yml/badge.svg)`
- [ ] 记忆/待办更新:project_urls 待办清除,仓库地址入档。

## 5. 亮相(可分日执行)

- [ ] dbt Community Slack(#tools-and-utilities)/ r/dataengineering / Hacker News
      "Show HN" 三选一先发一处,附 quickstart 与 accuracy 文档链接。
- [ ] 收集首批陌生人反馈 → 0.9.x 消化 → stability.md 去掉 draft 字样 → 1.0.0。
