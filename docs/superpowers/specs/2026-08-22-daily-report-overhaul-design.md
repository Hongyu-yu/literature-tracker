# 日报改造设计（2026-08-22）

四项改造：① 消灭「摘要信息不足」亮点降级；② 增加配图（免费 SVG 信息图 + 每篇小可视化 + Top-N 补海报）；③ 内容优先级偏向 AI×物化材（神经网络势/电子结构）> 纯物理（铁电铁磁多铁）> 其余；④ 每日图文 HTML 邮件发送到 594836947@qq.com。

本 spec 是执行蓝本，供 codex 按 TDD 实现。所有「文件:行号」基于 origin/main @ 2026-08-22（69b0d8bef）。

---

## 0. 全局约束与不变量（codex 必须遵守）

- **TDD**：每项改动先写/改测试，再改实现。用仓库自带 `python3 run_tests.py`（stdlib runner，本地无 pip；缺 bs4/PIL 等可选依赖会显式 skip，不算失败）。单测可脚本式 `python3 test_xxx.py` 单跑。
- **本地无网络/无网关**：新增 AI/图像调用**必须**可在无 key 时优雅降级（fail-soft），且测试**必须 mock** provider/image，不得真实联网。
- **仓库是公开的**：网关 IP/域名只存于 secret（`AI_HOSTS_ENTRY` 等），**绝不**硬编码进任何 `.py`/`.yml`/日志。
- **图像生成**：仅走 `image_provider.generate_image_b64`（gpt-5.5 Responses 流式 + `image_generation` tool）。已有 env 加固（`IMAGE_TIMEOUT_SECONDS`/`IMAGE_MAX_RETRIES`）。
- **网关 DNS 兜底**：凡新增会调 AI/图像的 workflow 步骤，其所在 job 必须已有 `echo "${{ secrets.AI_HOSTS_ENTRY }}" | sudo tee -a /etc/hosts` 步骤在前（fetch.yml/generate-deep.yml 已有，勿删）。
- **写盘格式**：`arxiv_core_<date>.json` 用 `json.dump(..., ensure_ascii=False)` 无缩进（匹配 run_deep）；`index.json` 保持 `indent=2`。
- **docs/data 永不入库**（.gitignore 第30行）；deploy job 部署期从 `data/` 复制。日报 HTML（docs/daily）入库。
- **推送**：race-safe（`git fetch` + `git rebase -X ours origin/main` + 重试循环，见 fetch.yml:93-105）。
- **无 gh CLI**：dispatch/验证 Actions 用 git-credential token + REST API。
- **成本旋钮**（本次定档）：Top-N 补海报 = **12 张/天**；亮点保障额外 AI ≤ 展示篇数（≤60/天，文本调用）。
- **成本约束落地**：所有新 AI/图像调用都要有 env 上限开关，可在 CI 里调 0 关闭。

---

## 任务 1：消灭「摘要信息不足」亮点降级

### 根因（已核实）
`research_context.py:202`：
```python
if not str(out.get("summary") or "").strip():
    out["summary"] = abstract or full or "摘要信息不足，需查阅原文确认具体方法与结论。"
```
`abstract`/`full` 只取中文字段（`abstract_zh`/`abstract_zh_full`，:195-196）。当 AI 亮点(`one_sentence_summary`)与中文摘要**都空**（网关故障批次被 `zh_enricher` 跳过 / 超出 `enrich_articles_zh` 每轮 `max_items=120` 的长尾 / 主列表 ≤60 篇未轮到富化）时，落到硬编码兜底——**从不回退英文 `abstract`**。全站日报出现 433 次。

### 决策：AI 补全 + 翻译兜底（绝不再吐「信息不足」）

分两层：

**层 A — 渲染兜底链改造（零额外成本，纯函数，必做）**
改 `research_context.ensure_relation_fields`（research_context.py:201-202）的 `summary` 兜底优先级为：
1. `one_sentence_summary`（AI 亮点，若有）
2. `abstract_zh`
3. `abstract_zh_full`
4. **英文 `abstract` 截断**（新增，约取前 200 字 + `…`）→ 保证「摘要始终存在」时永不落到第 5 步
5. 仅当 title/abstract/abstract_zh/full **全空**（纯元数据条目）时，用中性文案（如「本条目仅有出版元数据，完整信息请见原文。」——**不含「信息不足」字样**）

对应地，新增 helper `pick_summary(item) -> str` 纯函数封装该优先级，便于单测。

**层 B — 生成时「亮点保障」补全（AI，主质量提升）**
新增模块 `highlight_guarantee.py`（**确定：独立新模块**，保持单一职责，不并入 zh_enricher）：
- 函数 `ensure_highlights(items, *, provider, max_items, translate_fallback=True) -> int`
- 对**将在日报展示的**条目中「缺好中文亮点」者（无 `one_sentence_summary` 且无 `abstract_zh`），**批量**用其英文 `abstract` 生成 2~3 句中文亮点（新 prompt 外置 `ai_prompts/highlight_from_abstract.txt`，占位 `${items}`；要求：忠实摘要、不编造数值、≤120字、不得出现「信息不足/需查阅原文」等托词）。
- 失败/无 key/网关挂：逐条退化为 `translator.translate_text(abstract[:200])`（Google 翻译，已有依赖），仍失败才交给层 A 的英文截断。
- 幂等：已有亮点者跳过；`max_items` 上限（env `AI_HIGHLIGHT_MAX_ITEMS`，默认 60）。
- **接入点**：`generate_daily_pages.py` 的 `collect_daily_articles`（:943-985）拿到当日 `daily_articles`（≤60）后、送 AI 汇总前，调用 `ensure_highlights(daily_articles, ...)`；fail-soft（try/except，失败仅打印不中断）。

### 校验（禁止再生成兜底串）
- 新增测试断言：给定「有英文 abstract、无任何中文字段」的 item，`pick_summary` / `ensure_relation_fields` 产出的 `summary` **不含**「摘要信息不足」「需查阅原文确认具体方法与结论」，且非空、含英文或中文摘要内容。
- 新增测试：`ensure_highlights` 在 mock provider 正常时写入中文亮点；provider 抛错时走 translate_fallback（mock `translate_text`）；两者都挂时不写脏值、不抛异常、返回计数正确。
- 回归护栏：新增测试扫描 `research_context.py` 源码常量，确保「信息不足」不再作为默认 summary（或断言函数行为），防止回退。

### 涉及文件
`research_context.py`（改兜底链 + `pick_summary`）、`highlight_guarantee.py`（新）、`ai_prompts/highlight_from_abstract.txt`（新）、`generate_daily_pages.py`（接入 `collect_daily_articles`）、`test_research_context.py`（扩）、`test_highlight_guarantee.py`（新）。

---

## 任务 2：增加配图（免费图全套 + Top-N 补海报 12 张/天）

### 现状（已核实）
- 全站仅 1 处 `<img>`（深读海报，generate_daily_pages.py:553-556），每天仅 ~14–24 篇有图，列表最多 60 篇其余纯文字。
- **零统计图**：hero/侧栏所有数字都是纯文本（:798-814、:685-689），无 svg/chart/canvas/progress。
- 可用画图字段：`core_score`(0-1)、`focus_score`(0-10)、`ai_score`(0-10)、`classify_taxonomy`/`topic_bucket` 分类、`journal`、`arxiv_category`、`poster.elements`。

### 2A. 每日内联 SVG 信息图（零 AI 成本、必出、纯函数）
新增模块 `daily_viz.py`（纯函数，无外部依赖，输出 SVG 字符串；theme 用内联样式，兼容深/浅色）：
- `render_topic_distribution_svg(items) -> str`：按 `classify_taxonomy`/`topic_bucket` 统计条形图（AI×物理 / AI×化学·材料 / 磁性 / 铁电 / …）。
- `render_source_split_svg(items) -> str`：arXiv vs 期刊、来源构成（甜甜圈或堆叠条）。
- `render_priority_svg(items) -> str`：按任务3的优先级分层（P1 神经网络势/电子结构 / P2 铁电铁磁多铁 / P3 其余）计数条。
- 所有 SVG：`viewBox` 自适应、`max-width:100%`、`<title>`/`aria-label` 可访问、数字为 0 时优雅占位。
- **接入**：`generate_daily_pages.render_daily_html` 的 hero 区（:798 附近）插入一个「📊 今日概览」图表区块，横向排列上述 SVG（响应式，窄屏堆叠、`overflow-x:auto`）。

### 2B. 每篇免费小可视化（零成本，CSS/内联 SVG）
在 `render_unified_item`（generate_daily_pages.py:521-565）每张卡片加：
- **相关度条**：按 `focus_score`(0-10) 或 `core_score`(0-1) 画一个细条 + 数值徽标。
- **分类色标签**：`classify_taxonomy` 的分类做成带色 chip（P1/P2/P3 用不同色系）。
- 纯 CSS/inline-svg，不新增网络请求。样式加进现有 CSS（daily_page_enhancer 或 generate_daily_pages 的 `<style>`）。

### 2C. Top-N 补海报（12 张/天，有图像 API 成本）
复用现有海报能力，给**优先级 Top-N 篇但未深读**的论文补海报：
- 复用 `backfill_posters.py` 的「有 elements 走快路径、无 elements 走 generate_poster」，或 `poster_generator.generate_poster`。对未深读篇通常无 `poster.elements`，故走 `generate_poster(meta, src=abstract, provider=...)`（会先抽 5 要素再生图，每篇 1 次图 API + 少量文本）。
- **选取**：在 `generate-deep.yml` 现有 `run_deep.py`（深读，北京11:30）之后、`backfill_posters --max 12` **之前或之后**新增一步（或扩展 run_deep）：对当日 `daily_articles` 按任务3优先级排序，取未有 `image` 的 Top-12 生成海报，写回 `arxiv_core_<date>.json`（或新的 poster 挂载点，使 `build_unified_items` 能识别 `_enrich`/`image`）。
  - **实现（确定：新增独立脚本 `backfill_top_posters.py`）**，env `TOP_POSTER_MAX`（默认 12，可设 0 关闭）。它读当日 index/daily 集合 → 优先级排序 → 取未有图 Top-N → `generate_poster` → 写回当日 `arxiv_core_<date>.json`（若不存在则创建，schema 对齐 run_deep 的 tier2 记录：`{link, title_zh, deep_analysis?, image, poster{...}, source:"top_poster"}`）。
  - 复用每篇一次图 API 的幂等（已有 image 跳过）+ 预算递减 + `ThreadPoolExecutor` 并发（参照 backfill_posters `process_file`）。
- **接入 generate-deep.yml**：在 run_deep 之后加步骤 `python backfill_top_posters.py --max 12`（env 注入 AI/IMAGE key + hosts 已在前），随后已有的 `generate_daily_pages.py --rerender-only` 会把新图渲染进日报（:82）。

### 校验
- `test_daily_viz.py`（新）：各 SVG 函数返回合法 `<svg …>…</svg>`、含预期分类标签/计数、空输入不崩、数值正确（构造已知分布断言条形数值/占比）。
- `test_daily_pages_render.py`（扩）：渲染后的日报 HTML 含「今日概览」SVG 区块、每张卡片含相关度条/分类 chip；含图篇 `<img>` 仍在。
- `test_backfill_top_posters.py`（新）：mock `generate_poster`/`generate_and_save`，断言按优先级选 Top-N、已有图跳过、预算封顶、写盘无缩进、并发全部正确填充（参照 test_backfill_posters）。

### 涉及文件
`daily_viz.py`（新）、`backfill_top_posters.py`（新，或扩 backfill_posters.py）、`generate_daily_pages.py`（hero 图表 + 卡片小可视化 + CSS）、`.github/workflows/generate-deep.yml`（新步骤 + env）、`test_daily_viz.py`/`test_backfill_top_posters.py`（新）、`test_daily_pages_render.py`（扩）。

---

## 任务 3：内容优先级（AI×物化材 > 纯物理 > 其余），重排+重分组+接 focus_score

### 现状（已核实）
- 主列表排序（generate_daily_pages.py:415）：`items.sort(key=lambda x: (x.get("_tier",2), focus_priority(x)))`。
- `focus_priority`（focus_filter.py:420-448）主键是 `core_score`（focus_core 规则分）；**`focus_score`（对着 5 位学者真实画像的 LLM 分，最懂用户方向）根本没进主列表排序**，只在侧栏「🎯与你方向相关」小栏（render_focus_section:586-643）。
- `focus_core.TAXONOMY`：Tier1 = AI×物理 / AI×化学·材料；Tier2 = 磁性/铁电/拓扑/超导/量子。

### 决策：重排 + 重分组 + 接 focus_score（不硬删，全部仍展示）

**3A. `focus_core.py` 新增最高优先层（P1 术语簇 + 加权）**
在 TAXONOMY 增设一个 **tier 0**「神经网络势·电子结构」类别（或新增 `PRIORITY_TERMS`/`priority_score` 机制），命中即最高优先。术语簇（用户明确指定）：
- 方法/表征：`neural network potential`, `machine learning potential`, `mlip`, `interatomic potential`, `ml hamiltonian`, `learnable hamiltonian`, `density matrix`, `charge density`, `electron density`, `electronic structure`, `wavefunction`, `dft hamiltonian`, `equivariant`（+ 中文：神经网络势、机器学习势、原子间势、哈密顿量、密度矩阵、电荷密度、电子密度、电子结构、波函数）。
- 命中 P1 → `core_score` 额外 +0.20（或独立 `priority_tier=0`），确保稳居榜首。
- P2「纯物理·铁电铁磁多铁」= 现 Tier2 的铁电·极化 + 磁性·自旋（`ferroelectric/ferromagnet/antiferromagnet/multiferroic/铁电/铁磁/多铁…`）。P3 = 其余。
- 新增纯函数 `priority_tier(item) -> int`（0/1/2/3），供排序与分组共用。

**3B. 主列表排序接入 focus_score + 新优先层**
改 `focus_filter.focus_priority`（:420-448），在元组前部加入：
- 第一键改为 `priority_tier(item)`（0<1<2<3）
- 次键：`-focus_score`（画像分高者靠前；缺失视为 0 或从 index 补齐）
- 再次：`-core_score`、原有其余键保持
- `_tier`（富化优先）与 `priority_tier` 的先后：**确定优先级在前、富化其次**（先按研究相关性分层，层内再让有图深读的上浮），避免「有图但离题」压过「P1 但暂无图」。
- **单一真源（确定）**：`priority_tier` 作为 generate_daily_pages.py:415 排序的**最外层键**；`focus_priority`（focus_filter）**不再重复计入 priority_tier**，只负责层内排序（把 `-focus_score` 作为其新的首要子键，其余键保持）。最终排序键 = `(priority_tier(x), x.get("_tier",2), focus_priority(x))`。

**3C. 扩大 focus_score 覆盖**
现状 `enrich_focus_interest` 只跑当日**新文章** ≤20（run_optimized_sync.py:282-289）。改为对**将进入日报的 daily 集合**（≤60）补齐缺 focus_score 者：
- 在 `generate_daily_pages.collect_daily_articles` 拿到 daily_articles 后，对缺 `focus_score` 者调用 `enrich_focus_interest(subset, provider, max_items=env AI_FOCUS_DAILY_MAX 默认60)`，fail-soft。
- 与任务1的 `ensure_highlights` 同一接入点，可合并为一次「日报富化保障」调用块。

**3D. 重分组（日报可视分层）**
`group_daily_items`（generate_daily_pages.py:200 附近，按 topic_bucket 分 physics/chemistry/materials/methods）改为**先按 priority_tier 分大组**：
- 「🔬 神经网络势 · 电子结构（重点）」(P1)
- 「🧲 铁电 · 铁磁 · 多铁（物理）」(P2)
- 「🧩 其他交叉 / 方法」(P3)
- 每大组内保留原 topic 细分或直接按 focus_score 排。标题/描述更新。

### 校验
- `test_focus_core.py`（扩）：`priority_tier` 对「neural network potential + electronic structure」返回 0；「ferroelectric」返回 2；无关返回 3；P1 命中使 `core_score` 提升。
- `test_focus_filter.py`（扩）：`focus_priority` 使 P1 排在 P2 前、P2 排在 P3 前；`focus_score` 高者在同层靠前。
- `test_daily_pages_render.py`（扩）：日报出现三个优先级分组标题；P1 论文排在最前。
- 覆盖 fail-soft：focus 富化抛错时日报仍生成。

### 涉及文件
`focus_core.py`（P1 术语簇 + `priority_tier` + 加权）、`focus_filter.py`（focus_priority 接入）、`generate_daily_pages.py`（排序键 + `group_daily_items` 重分组 + focus_score 覆盖）、`test_focus_core.py`/`test_focus_filter.py`/`test_daily_pages_render.py`（扩）。

---

## 任务 4：每日图文 HTML 邮件 → 594836947@qq.com（深读后发，~中午12:00）

### 现状（已核实）
- 收件人 `594836947@qq.com`、`smtp.qq.com:465`、mode`digest` 已在 config.py:128-135；secrets `EMAIL_SENDER`/`EMAIL_PASSWORD` 已存在。
- `EmailNotifier`（email_notifier.py）已写好（SSL 465、full/digest），但**只被遗留 main.py 调用**（按 Article **对象**，字段无亮点/图），任何 workflow 都不跑它 → **一封都不发**。

### 决策：富 HTML 图文，每日一封，从 generate-deep.yml 深读重渲染后发

**4A. dict→HTML 邮件适配器**
新增模块 `daily_email.py`（或给 EmailNotifier 加 dict 分支）：
- `build_daily_email_html(summary: dict, day_str: str, site_base: str) -> (subject, html)`：
  - 复用日报 `summary`（含 core_items/focus_items/亮点）+ 当日 daily_articles。
  - 结构：标题「📚 每日文献日报 · {day_str}」；顶部一段「今日概览」（可复用 `daily_viz` 的 SVG，或纯文本计数——注意邮件客户端对 inline SVG 支持不一，**海报缩略图用 https `<img>` 更稳**）；然后 Top 亮点卡片列表：每条 = 中文标题 + `💡 亮点`(pick_summary 结果) + 优先级/分类 chip + 「阅读原文」链接；含图篇加 `<img src="{site_base}/images/posters/{doc_id}.webp" width=…>`（gh-pages 绝对 https）。
  - 底部大按钮「查看完整图文日报」→ `{site_base}/daily/{day_str}.html`。
  - `site_base` = `https://hongyu-yu.github.io/literature-tracker`（可 env `SITE_BASE_URL` 覆盖）。
  - 缩略图数量上限 env `EMAIL_POSTER_MAX`（默认 5）。webp 在部分客户端不显示 → `<img>` 加 `alt` 与链接兜底；可接受（QQ 邮箱支持 webp）。
- `send_daily_email(summary, day_str)`：读 `EMAIL_CONFIG`，缺 `sender_email`/`sender_password` 时**跳过并打印**（不报错、不泄密）；用现有 `EmailNotifier` 的 SMTP 发送能力（复用其 `_send`/连接逻辑，或在 EmailNotifier 加 `send_html(recipient, subject, html)` 方法）。

**4B. 接入 generate-deep.yml（深读后一次）——确定：flag 方案**
- 给 `generate_daily_pages.py` 加 `--send-email` flag：`--rerender-only` 重渲染当日 summary 后，**复用同一 summary** 调 `daily_email.send_daily_email(summary, day_str)`，避免重复构建。
- 把 generate-deep.yml:82 步骤改为 `python generate_daily_pages.py --rerender-only --days 4 --send-email`，该步 env 增注 `EMAIL_SENDER`/`EMAIL_PASSWORD`/`SITE_BASE_URL`（AI/hosts 已在该 job）。
- 幂等/防重：同一天只发一封——发送前检查 `data/email_sent.json`（记录已发日期；`data/` 不入 docs 但入库 main，可作跨 run 标记）；env `EMAIL_ENABLED`（默认1）可关。

**4C. 发送内容日期**
发「北京时间昨天」的日报（与 generate_daily_pages 默认 offset 一致，:1026），确保该日 HTML 与海报已在本次 run 内生成。

### 校验
- `test_daily_email.py`（新）：`build_daily_email_html` 对样本 summary 产出含亮点、含 `daily/{day}.html` 链接、含 `images/posters/*.webp` 绝对 https `<img>`、subject 正确；空 summary 优雅处理；**不含**「信息不足」。
- 发送路径：mock `smtplib.SMTP_SSL`，断言 `send_daily_email` 在有 creds 时调用发送、无 creds 时跳过不抛、防重标记生效。**测试绝不真实发信**。
- 密钥安全：断言 HTML/日志不含 sender_password。

### 涉及文件
`daily_email.py`（新）、`email_notifier.py`（可加 `send_html`）、`generate_daily_pages.py`（`--send-email` + 防重）、`send_daily_email.py`（可选薄入口）、`.github/workflows/generate-deep.yml`（新步骤 + env）、`config.py`（可加 `SITE_BASE_URL` 默认）、`test_daily_email.py`（新）。

---

## 测试与验收（整体）

1. **本地全绿**：`python3 run_tests.py` 全通过（新增测试 + 现有回归），缺依赖显式 skip 不算失败。所有新 AI/图像/SMTP 调用在测试中被 mock，零真实联网。
2. **推送**：codex 在 feature 分支实现 → 本地测试绿 → 合并/推 main（race-safe rebase）。spec 本文件随之入库。
3. **线上验证**（推送后）：
   - dispatch `fetch.yml` + `generate-deep.yml`（REST API），completed success。
   - 抓 runner 日志确认：`NameResolutionError`/`摘要生成失败` = 0；「亮点保障」「Top-N 海报」「focus 富化」「邮件发送成功」正向证据出现。
   - 拉取 origin/main 最新 `docs/daily/<昨天>.html`，断言：**全站「摘要信息不足」计数为 0**；出现「今日概览」SVG、三优先级分组、每篇相关度条；含图篇数明显增加（深读 + Top-12）。
   - 邮件：日志出现「✅ 邮件发送成功 → 594836947@qq.com」且无 SMTP 异常（无法读收件箱，以发送端日志为准；提示用户查收）。
4. **回归护栏**：新增测试确保「信息不足」串不再作为默认亮点，防止未来回退。

## 风险与出界

- **成本**：Top-12 海报 = 12 次图像 API/天 + 亮点/画像补全 ≤60 文本调用/天；均有 env 开关可调 0。
- **webp 邮件兼容**：QQ 邮箱支持；其他客户端可能不显示缩略图，靠 alt + 「查看完整日报」按钮兜底。
- **不在本次范围**：APS 全文源 `59.110.144.56` 宕机（用户已选忽略）；周报（仅在共享函数被复用时顺带受益，不主动改版式）；`main.py` 遗留发信路径（不动，避免双发）。
- **单一真源**：`priority_tier` 只实现一次（focus_core），排序与分组共用；`pick_summary` 只实现一次（research_context），渲染与邮件共用。
