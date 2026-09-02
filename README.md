# 📚 文献追踪系统(literature-tracker)

自动追踪 50+ 学术期刊 RSS(Nature/Science/APS/ACS/Wiley/RSC/Elsevier/arXiv),
按关键词与 AI 相关性筛选 ML × 铁电/凝聚态交叉文献,生成中文日报/周报与深读分析,
经 GitHub Pages 发布。全流程由 GitHub Actions 驱动,无需本地常驻服务。

## 架构与数据流

```
RSS 源 ──run_optimized_sync.py──▶ data/(history.json、index.json 等,唯一事实源)
                │                        │
                │ AI 相关性筛选/中文富化   ├─▶ articles/*.md(每篇文献一个文件)
                │ (同一次 LLM 调用产出     │
                │  abstract_zh 浓缩摘要 +  │
                │  abstract_zh_full 完整翻译)│
                ▼                        │
   阶段4.5:focus_interest.py 兴趣画像匹配
        (规则预筛 + LLM 批量,画像来自
         data/focus_interests.json)──▶ index.json 新增
         focus_score/focus_summary/focus_relation/focus_suggestion
   阶段4.6:cross_relevance.py 双维相关度判定(同一次 LLM 调用产出两个分)
        (规则分层 rule_cross_tier 0-3 零成本 + LLM 打分)
        ──▶ cross_score/cross_reason  AI×物理/材料/化学 的交叉强度
            me_score/me_reason        与主学者本人画像(data/focus_interests.json
                                      的 primary 块)的相关度
        **cross_score 是日报选文、截断、分区与邮件选卡的主排序键**
        **两分双双过线 = 强相关，周报非顶刊闸门用它**
   generate_daily_pages.py ──▶ docs/daily/*.html(日报,按「AI×科学交叉 / 其他物理材料」分区)
   run_deep.py(APS 全文深读 + arXiv 富化 + 海报)──▶ data/aps_*.json、docs/images/posters/
   weekly_summary.py ──▶ docs/weekly/*.html(周报,含同款 focus 区块)
   update_focus_profile.py(本地抓取提交 works;dispatch workflow 只做蒸馏)──▶ data/focus_interests.json
        (含 scholars[].keywords 逐人关键词 + primary 主学者单独画像;
         团队并集 205 词里 155 个来自另四位学者,判「与本人强相关」不能用并集)
        (抓 Google Scholar 近五年论文 → S2/OpenAlex/arXiv 回填摘要 → LLM 蒸馏;
         CI 用 --distill-only 不重抓 Scholar,防云端 IP 被封)
                                         │
            部署:deploy job 将 data/* 复制到 docs/data/ 后上传 Pages artifact
            (docs/data/ 不入库——这是 2026-06 优化批确立的约束)
```

前端 `docs/`:单页应用(index.html + app.js),虚拟滚动 / IndexedDB 缓存 /
倒排索引搜索 / PWA(sw.js,相对路径注册);analytics.html 为数据分析页。

## GitHub Actions 一览

| Workflow | 触发(UTC) | 作用 |
|---|---|---|
| fetch.yml | 00:00、12:00 + 手动 | 抓取→筛选→富化→日报,提交并自部署 |
| generate-deep.yml | 06:30 + 手动 | APS/arXiv 深读、海报、重渲染日报、发日报邮件 |
| weekly-summary.yml | 周日 01:00 + 手动 | 周报(01:00 为错峰 fetch,勿改回 00:00) |
| deploy-on-push.yml | push main | 其余推送的 Pages 部署(消息前缀过滤自动提交) |
| smoke.yml | push/PR | py_compile + run_tests.py + 脚本式测试 |
| backfill-daily / backfill-weekly / backfill-zh | 手动 | 历史回填 |
| update-focus-profile.yml | 手动 | 研究兴趣画像蒸馏(`--distill-only`,不重抓 Scholar) |
| test.yml | 手动 | 5 个 RSS 源快速连通性验证 |

工程约束由 `test_actions.py` 固化(浅克隆 fetch-depth:1、push 必带 5 次 rebase
重试、部署前复制 docs/data、全 job 超时、cron 不撞车),smoke 会在 CI 强制执行。

## 周报的两级期刊门槛

- **顶刊**（`weekly_summary.TOP_JOURNALS`,26 项人工策展,含 Nature/Science 系、PRL、PRX、Adv. Mater.、arXiv）: 相关即可,沿用宽松关键词 + AI 二元判定。
- **非顶刊**（含 Preprints/Research Square 等预印本平台）: 必须**与主学者强相关** ——
  `cross_score` 与 `me_score` 双双 ≥ `STRONG_MIN_SCORE`(默认 7)。
  打分发生在闸门之前(每周约 38 次调用,`AI_CROSS_WEEKLY_MAX` 可关)。
  无 AI 分时退回关键词规则兜底,**取严不取宽**:标题命中 ≥2 个本人专属关键词
  且是标题级交叉才放行(实测该周 300 篇非顶刊候选放行 5 篇)。
  画像缺 `primary` 块时非顶刊一律不放行(回到"只收顶刊"的老行为),并打印告警。

## 本地开发与测试

本机无需安装依赖即可跑大部分测试(stdlib 降级):

```bash
python3 run_tests.py            # 顶层 test_* 函数;缺 bs4/json_repair 的用例显式 skip
python3 test_daily_pages_render.py   # 脚本式测试(断言在 main() 里),逐个独立运行
```

约定:**新测试一律写成顶层 `test_*` 函数**(unittest.mock + tempfile,
不用 pytest fixture),使 run_tests.py 与 CI pytest 都能执行;
脚本式 main() 测试为历史存量,smoke.yml 中单独逐个运行。

完整环境(CI 等价):`pip install -r requirements.txt` 后同样命令全量执行。

## 配置

- `config.py`:RSS 源、关键词、邮件;敏感项放 `config.local.py`(已 gitignore)
- AI 相关环境变量(CI Secrets):`AI_PROVIDER/AI_MODEL/AI_API_KEY/AI_BASE_URL`、
  `KIMI_*`、`NOTION_*`、`TG_LIT_*`、`APS_HTTP_*`(私有全文源)
- 细节见 [README_CONFIG.md](README_CONFIG.md) 与 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)

## 目录结构

```
├── run_optimized_sync.py / generate_daily_pages.py / run_deep.py / weekly_summary.py
│                          # 四个 CI 入口(其余模块被它们引用)
├── data/                  # 唯一事实源(history/index/ai_relevant/深读缓存)
├── articles/              # 文献 Markdown 库(按日期_哈希命名)
├── docs/                  # GitHub Pages 站点(daily/ weekly/ images/ 为生成物;
│                          #   docs/data/ 部署期复制,不入库)
├── docs/superpowers/      # 设计 specs 与实施 plans(含本次优化的完整方案)
├── archive/               # 历史施工总结(只读参考)
└── .github/workflows/     # 9 个 workflow(见上表)
```

## 历史文档

V3–V5.1 各阶段功能清单与施工总结见 [archive/](archive/);
开发期规范文档见 `.kiro/specs/`。

## 许可证

MIT License
