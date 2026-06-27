# 概念海报「图内嵌入中文详细要素」设计

- 日期:2026-06-27
- 状态:已批准,待写实现计划
- 分支:feat/poster-chinese-infographic

## 背景与问题

当前概念海报生图链路(`poster_generator.py: build_infographic_prompt`)刻意走「全英文短标签」路线:
生图只用 `elements_en`(每要素 ≤6 个单词的英文短标签),且 prompt 内硬写
`use ONLY short English words as labels — NO Chinese characters`。

后果:图里只有 5 个英文短词 + 几个示意图标,信息量极低,**看图根本不知道论文讲啥**。
而提取阶段其实已经产出了中文 5 要素(`parsed["elements"]`),但**生图时完全没用**——
只在网页里作为图旁文字卡(`generate_daily_pages.py:515-521` 的 `daily-deep-elements`)展示,
与空洞的图割裂。

当初改成全英文,几乎可以肯定是为了规避图模型渲染中文乱码。但这背离了用户原始意图:
用户要的是「图内嵌入中文详细内容、看图即懂」,且原 prompt 已用 SimSun + "清晰无乱码" 来对抗乱码。

## 目标

生图改为**照搬用户原 prompt**:图内嵌入 5 要素的详细中文内容 + 用户整套设计要求
(现代极简科技信息图 / 孟菲斯 / 深学术蓝+板岩灰+橙青 / 16:9 / Times New Roman+SimSun 等),
做到「看图即懂」。图旁已有的中文文字卡**保留作兜底**。

## 用户已确认的决策

- 图内文字策略:**图内嵌入详细中文(照搬原 prompt)**,而非英文短标签。
- 图内中文量:**不限字数,完全照搬原 prompt**(用户已知并接受乱码风险)。
- 生效范围:**先小样验证 + 以后生效**(不批量回填历史海报)。

## 方案

### A. 生图 prompt 外置(`ai_prompts/poster_image.txt`,新增)

把用户第二段生图 prompt **原文**放入该文件,占位符:
- `${summaryForImage}`:中文 5 要素拼成的整段文本。
- `${title}`:论文标题。
- `${language}`:输出语言(默认「中文」)。

原文要点(完整收录用户给定文案):学术论文概念图;研究问题/创新方法/工作流程/关键结果/应用价值;
设计要求(艺术风格:Modern Minimalist Tech Infographic / flat vector + isometric / 企业级孟菲斯 /
clean lines；构图:居中或左→右流程 / 干净米白或浅灰背景 #F5F5F7 / 像 PPT 或学术海报那样组织；
配色:深学术蓝 & 板岩灰,活力橙或青强调,高对比专业调色；文字渲染:英文 Times New Roman、
中文 SimSun、主语言 ${language}、标题不必出现在图中、中文需清晰无乱码；
负面提示:不要照片写实 / 草图 / 模糊文字 / 混乱背景;生成 16:9、4K)。

### B. `poster_generator.py`

- 新增 `_build_summary_for_image(elements_zh)`:把中文 5 要素拼成
  `1. 研究问题:…\n2. 创新方法:…\n3. 工作流程:…\n4. 关键结果:…\n5. 应用价值:…`(缺项跳过)。
- 重写 `build_infographic_prompt`:签名 `(elements_en, title)` → `(elements_zh, title, language="中文")`;
  改为读 `poster_image.txt` 模板(`string.Template.safe_substitute`)并填入
  `summaryForImage`(= `_build_summary_for_image(elements_zh)`)/ `title` / `language`;
  **删除**原先 `NO Chinese characters / ONLY short English` 等英文-only 逻辑。
- `generate_poster`:改为把中文要素(`parsed["elements"]`)传给 `build_infographic_prompt`;
  压缩 `max_edge` 由 **1280 → 1536**(图内含中文,边长太小会糊;webp 体积仍可控)。

### C. 提取模板(`poster_elements.txt`)

不动。它已提取中文 5 要素 + `title_zh`,正好作为 summaryForImage 的来源。
`elements_en` 字段保留(无害;生图不再使用,网页文字卡用的是中文 keys)。

### D. 小样验证(上线前,非代码)

改完后对最近 1~2 篇**实际跑 `generate_poster` 出图**,人工肉眼检查中文渲染效果:
- 本地若有图 API key(`IMAGE_API_KEY`/`AI_API_KEY` + base)→ 本地跑脚本生成 1~2 张 webp。
- 本地无 key → 临时在 CI 限量跑(只对 1~2 篇 dispatch)。

**验证通过(中文基本可读)才**让以后新文章自动用新 prompt。若效果差,回退到「每要素一句话精简中文」
(在 `_build_summary_for_image` 截断每要素 ≤20 字)是一行参数级的低成本调整。

## 数据流

深读/全文 → 提取中文 5 要素(不变)→ `_build_summary_for_image` 拼整段
→ 填 `poster_image.txt` 模板 → gpt-5.5(Responses 流式 image_generation)出图
→ 压 webp(max_edge 1536)→ 网页:图 + 中文文字卡兜底。

## 测试

`test_poster_generator.py`(纯字符串断言,不真实调 API):
- `build_infographic_prompt` 输出含中文要素文本(如「研究问题」字样)+ 设计要求关键词
  (「孟菲斯/Memphis」「SimSun」「#F5F5F7」「16:9」其一组),且**不再**含 `NO Chinese` / `English only` / `ONLY short English`。
- `_build_summary_for_image` 对给定中文 5 要素拼接顺序/编号正确;缺项跳过不报错。
- `poster_image.txt` 模板文件存在且含三个占位符。
- 全量 `python run_tests.py` 不回归。

## 不动(既有不变量)

- 提取 5 要素链路(`extract_elements` / `poster_elements.txt`)。
- 网页中文文字卡(`daily-deep-elements`)。
- `focus_core` 判定与计分、深读正文与信息图编排(`run_deep.py`)。
- `docs/data` 不入库;SW 相对路径 + 改前端 bump 版本;图像生成仅走 gpt-5.5 Responses 流式。

## 风险与边界(诚实说明)

- gpt-5.5 对**不限量中文**渲染大概率有部分糊字/错字——模型能力限制,prompt 的 SimSun +
  "清晰无乱码" 只能引导、不能保证。用户已知并接受;靠小样验证先看效果 + 文字卡兜底。
- max_edge 提到 1536 会增大单图 webp 体积(约 1.4×),`docs/images/posters` 总量随之上升;
  可接受,且仅影响新生成的图。
- 每篇重生成海报 = 一次画图 API 调用,有成本;故**不批量回填**,仅小样验证 + 以后生效。
