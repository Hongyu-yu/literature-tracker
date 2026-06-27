# 概念海报「图内嵌入中文详细要素」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 概念海报生图从「英文短标签示意图」改为「图内嵌入中文详细 5 要素 + 用户整套设计要求」,做到看图即懂。

**Architecture:** 生图 prompt 外置成 `ai_prompts/poster_image.txt`(照搬用户原文,占位 `${summaryForImage}/${title}/${language}`);`poster_generator.py` 把已提取的中文 5 要素拼成 summaryForImage 文本填入模板,删除原英文-only 逻辑;压缩边长提至 1536 保中文清晰。

**Tech Stack:** Python 3.11 stdlib-only(本地无 pip);`string.Template`;现有 gpt-5.5 Responses 流式 image_generation(`image_provider.py`,不改)。

## Global Constraints

- 本地测试用 `python run_tests.py`(stdlib runner,缺依赖降级 skip);纯 prompt 断言不得触发 PIL/网络。
- 提取链路(`extract_elements`/`poster_elements.txt`)、网页中文文字卡(`daily-deep-elements`)、`focus_core` 计分、`run_deep.py` 编排:**不动**。
- `docs/data` 不入库;图像生成仅走 gpt-5.5 Responses 流式;`max_edge` 新值 **1536**。
- `_KEYS = ["研究问题","创新方法","工作流程","关键结果","应用价值"]`(已存在,复用)。
- 模板占位符固定三个:`${summaryForImage}`、`${title}`、`${language}`。

---

### Task 1: 生图 prompt 改为图内嵌中文(模板外置 + 重写 builder + 改测试)

**Files:**
- Create: `ai_prompts/poster_image.txt`
- Modify: `poster_generator.py`(新增 `_IMAGE_PROMPT_PATH`、`_build_summary_for_image`;重写 `build_infographic_prompt`;`generate_poster` 改传中文要素 + `max_edge=1536`)
- Test: `test_poster_generator.py`(改写 2 个旧断言 + 新增 3 个)

**Interfaces:**
- Consumes: `extract_elements(...)["elements"]`(中文 5 要素 dict,键为 `_KEYS`)。
- Produces:
  - `_build_summary_for_image(elements_zh: dict) -> str` — 返回 `1. 研究问题:…\n2. 创新方法:…`(缺项跳过,编号按 `_KEYS` 顺序)。
  - `build_infographic_prompt(elements_zh: dict, title, language="中文") -> str` — 读模板填占位返回完整生图 prompt。
  - `poster_generator._IMAGE_PROMPT_PATH` — 模板绝对路径。

- [ ] **Step 1: 创建生图 prompt 模板(照搬用户原文)**

Create `ai_prompts/poster_image.txt`:

```
根据"${summaryForImage}"，生成一张学术论文概念图，清晰展示以下内容：

研究问题：提到的核心问题
创新方法：论文提出的主要方法或技术
工作流程：从输入到输出的处理流程
关键结果：主要实验发现或性能提升
应用价值：该研究的实际意义
论文标题：${title}
要求：
**设计要求 (Design Guidelines - STRICTLY FOLLOW):**
1.  **艺术风格 (Style):**
    *   Modern Minimalist Tech Infographic (现代极简科技信息图).
    *   Flat vector illustration with subtle isometric elements (带有微妙等距元素的扁平矢量插画).
    *   High-quality corporate Memphis design style (高质量企业级孟菲斯设计风格).
    *   Clean lines, geometric shapes (线条干净，几何形状).
2.  **构图 (Composition):**
    *   **Layout:** Central composition or Left-to-Right Process Flow (居中构图或从左到右的流程).
    *   **Background:** Clean, solid off-white or very light grey background (#F5F5F7). No clutter. (干净的米白或浅灰背景，无杂乱).
    *   **Structure:** Organize elements logically like a presentation slide or a academic poster.
3.  **配色方案 (Color Palette):**
    *   Primary: Deep Academic Blue (深学术蓝) & Slate Grey (板岩灰).
    *   Accent: Vibrant Orange or Teal for highlights (活力橙或青色用于高亮).
    *   High contrast, professional color grading (高对比度，专业调色).
4.  **文字渲染 (Text Rendering):**
    *   Use Times New Roman font for English.
    *   Use SimSun font for Chinese.
    *   Main text language: ${language} (User defined language).
    *   The title does not need to be reflected in the figure.
    *   The text, especially Chinese, needs to be clear and free of garbled characters.
5.  **负面提示 (Negative Prompt - Avoid these):**
    *   No photorealism (不要照片写实风格).
    *   No messy sketches (不要草图).
    *   No blurry text (不要模糊文字).
    *   No chaotic background (不要混乱背景).
**Generation Instructions:**
Generate an academic infographic poster with a width of 16:9 with 4K resolution.
```

注意:文件内**仅**这三个 `${...}` 是占位符,其余无 `$`,`safe_substitute` 安全。负面提示里的英文 "No ... background" 等是设计要求的一部分,**保留**(它们不是"禁中文"指令)。

- [ ] **Step 2: 改写测试(先让其失败)**

在 `test_poster_generator.py`:

(a) 把 line 22-26 的 `test_infographic_prompt_is_text_free_memphis` 整体替换为:

```python
def test_infographic_prompt_embeds_chinese_summary():
    p = build_infographic_prompt(
        {"研究问题": "如何加速材料筛选", "创新方法": "图神经网络势函数"}, "Some Title")
    assert "图神经网络势函数" in p          # 中文详细内容嵌入图 prompt
    assert "研究问题" in p
    assert "SimSun" in p                      # 用户设计要求关键词
    assert "16:9" in p
    assert "no chinese" not in p.lower()      # 不再禁中文
    assert "only short english" not in p.lower()
```

(b) 把 line 67-73 的 `test_infographic_prompt_is_readable_english_no_chinese` 整体替换为:

```python
def test_infographic_prompt_has_design_guidelines_and_title():
    p = build_infographic_prompt({"创新方法": "扩散模型生成晶体"}, "Crystal Title")
    assert "扩散模型生成晶体" in p
    assert "Memphis" in p or "孟菲斯" in p
    assert "#F5F5F7" in p
    assert "Crystal Title" in p
```

(c) 在文件末尾追加 3 个新测试:

```python
def test_build_summary_for_image_numbered_and_skips_empty():
    from poster_generator import _build_summary_for_image
    s = _build_summary_for_image(
        {"研究问题": "Q", "创新方法": "M", "工作流程": "", "关键结果": "R", "应用价值": ""})
    assert "1. 研究问题：Q" in s
    assert "2. 创新方法：M" in s
    assert "工作流程" not in s          # 空值跳过
    assert "应用价值" not in s
    assert len(s.splitlines()) == 3

def test_poster_image_template_exists_with_placeholders():
    import poster_generator as pg
    with open(pg._IMAGE_PROMPT_PATH, encoding="utf-8") as f:
        t = f.read()
    assert "${summaryForImage}" in t and "${title}" in t and "${language}" in t

def test_build_summary_for_image_empty_dict():
    from poster_generator import _build_summary_for_image
    assert _build_summary_for_image({}) == ""
    assert _build_summary_for_image(None) == ""
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python run_tests.py test_poster_generator`
Expected: FAIL — `_build_summary_for_image` / `_IMAGE_PROMPT_PATH` 不存在;新断言不满足。

- [ ] **Step 4: 实现 poster_generator.py 改动**

在文件顶部 `_PROMPT_PATH = ...` 行下方新增模板路径常量:

```python
_IMAGE_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "poster_image.txt")
```

新增 `_build_summary_for_image`(放在 `_parse_elements` 之后):

```python
def _build_summary_for_image(elements_zh):
    elements_zh = elements_zh or {}
    lines = []
    for i, k in enumerate(_KEYS, 1):
        v = str(elements_zh.get(k, "") or "").strip()
        if v:
            lines.append(f"{i}. {k}：{v}")
    return "\n".join(lines)
```

整体替换现有 `build_infographic_prompt`(line 43-58)为:

```python
def _load_image_template():
    with open(_IMAGE_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()

def build_infographic_prompt(elements_zh, title, language="中文"):
    summary = _build_summary_for_image(elements_zh)
    return Template(_load_image_template()).safe_substitute(
        summaryForImage=summary, title=str(title or ""), language=language)
```

修改 `generate_poster` 内两行:把
```python
    prompt = build_infographic_prompt(parsed["elements_en"], meta.get("title", ""))
    saved = generate_and_save(prompt, out_path, max_edge=1280,
                              api_key=api_key, base=base)
```
改为
```python
    prompt = build_infographic_prompt(parsed["elements"], meta.get("title", ""))
    saved = generate_and_save(prompt, out_path, max_edge=1536,
                              api_key=api_key, base=base)
```

(返回 dict 仍保留 `elements_en` 字段不变,网页/未来可用。)

- [ ] **Step 5: 跑测试确认通过**

Run: `python run_tests.py test_poster_generator`
Expected: PASS(全部 poster_generator 测试)。

- [ ] **Step 6: 跑全量回归**

Run: `python run_tests.py`
Expected: 无新增 FAIL(缺依赖项照常 skip)。

- [ ] **Step 7: 提交**

```bash
git add ai_prompts/poster_image.txt poster_generator.py test_poster_generator.py
git commit -m "feat(poster): 概念海报图内嵌入中文详细要素,照搬用户设计prompt"
```

---

### Task 2:(人工)小样验证中文渲染

> 非 TDD;需图 API key,实际出图肉眼检查。验证通过才让以后新文章自动用。

- [ ] **Step 1: 确认本地是否有图 API key**

Run: `python3 -c "import os;print(bool(os.environ.get('IMAGE_API_KEY') or os.environ.get('AI_API_KEY')), bool(os.environ.get('IMAGE_API_BASE') or os.environ.get('AI_BASE_URL')))"`

- [ ] **Step 2A:(本地有 key)对 1~2 篇实际出图**

写临时脚本 `scratchpad/poster_probe.py`:取最近一篇 enrich 记录(从 `data/index.json` 找带 `deep_analysis` 或 `poster.elements` 的条目),用其中文 5 要素构造 `meta`+markdown,调 `poster_generator.generate_poster(...)` 生成到 `scratchpad/probe_posters/`。用 Read 工具看生成的 webp,人工判断中文是否基本可读、无大面积乱码。

- [ ] **Step 2B:(本地无 key)CI 限量验证**

经 git-credential token + REST API `workflow_dispatch` 触发 `generate-deep.yml`(或 backfill),**限定只跑最近 1 天/1~2 篇**(用其 days/limit 类入参,或临时加 `--limit`)。等运行完,curl 线上对应海报图,Read 查看。

- [ ] **Step 3: 判定**

- 中文基本可读 → 验证通过,以后新文章自动用新 prompt(无需额外改动,Task 1 已生效)。在 spec/账本记录"验证通过"。
- 糊字严重 → 低成本回退:在 `_build_summary_for_image` 给每要素值加 `[:20]` 截断(每要素一句话),重跑 Step 2 复验。

---

## Self-Review

- **Spec coverage:** A(模板外置)→Task1 Step1;B(builder/summary/max_edge)→Task1 Step4;C(提取不动)→未改;D(小样验证)→Task2。✓
- **Placeholder scan:** 无 TBD;所有步骤含确切代码/命令。✓
- **Type consistency:** `_build_summary_for_image(dict)->str`、`build_infographic_prompt(elements_zh,title,language)`、`_IMAGE_PROMPT_PATH` 在 Task1 内一致引用;测试用同名。✓
- **旧测试冲突:** 已显式改写 line 22-26 与 67-73 两处断言旧行为的测试。✓
