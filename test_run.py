#!/usr/bin/env python3
"""RSS 连通性冒烟脚本（手动运行，不是单元测试）。

由 .github/workflows/test.yml 手动 dispatch，用来确认「RSS 抓取 + 关键词筛选 + 翻译」
这条链路还通。只读不写：**绝不碰 data/ 与 articles/**。

历史教训（两处，都已修）：
1. 模块体没有 __main__ 守卫，而 run_tests.py 会 import 所有 test_*.py —— 于是每次
   push/PR 的 smoke 都会真的去抓 5 个线上 RSS 并重写 13MB 的 data/index.json。
2. 它曾通过 DataManager 往真实的 data/ 和 articles/ 里写入，而 test.yml 又会把结果
   commit 进 main，任何人 dispatch 一次就污染线上数据集。现在只做连通性检查，
   一个字节都不落盘（也因此不再依赖已删除的 data_manager.py）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 只用 5 个源，够验证连通性即可
TEST_FEEDS = [
    "https://www.nature.com/nphys.rss",
    "https://www.nature.com/natmachintell.rss",
    "https://www.nature.com/ncomms.rss",
    "http://feeds.aps.org/rss/recent/prl.xml",
    "http://feeds.aps.org/rss/recent/prb.xml",
]

KEYWORDS = ["ferro", "machine", "learning", "magne", "neural", "network", "potential", "hamiltonian"]


def main() -> int:
    try:
        from rss_fetcher import RSSFetcher
        from translator import translate_text
    except Exception as e:
        print(f"❌ 导入失败: {type(e).__name__}: {e}")
        return 1

    print(f"🧪 RSS 连通性冒烟 — {len(TEST_FEEDS)} 个源（只读，不写任何文件）")

    fetcher = RSSFetcher(KEYWORDS)
    articles = fetcher.fetch_all(TEST_FEEDS)
    print(f"📡 共获取 {len(articles)} 篇")

    # fetch_all 末尾已经打印过每源健康报告；这里再显式判定一次成败
    health = getattr(fetcher, "feed_health", []) or []
    broken = [h for h in health if h.get("problem")]
    if broken:
        print(f"❌ {len(broken)}/{len(health)} 个源报错：")
        for h in broken:
            print(f"   {h['url']} → {h['problem']}")
        return 1
    if not articles:
        print("❌ 所有源都没有返回条目——连通性异常（正常情况下 5 个大刊不会同时无更新）")
        return 1

    filtered = fetcher.filter_by_keywords(articles)
    print(f"🔍 关键词+领域筛选后 {len(filtered)} 篇")

    # 翻译链路只验证一条，避免手动冒烟也去打一堆翻译请求
    sample = (filtered or articles)[0]
    zh = translate_text(sample.title)
    print(f"🌐 翻译样例:\n   EN: {sample.title[:70]}\n   ZH: {str(zh)[:70]}")
    if not str(zh or "").strip():
        print("❌ 翻译返回空")
        return 1

    print("✅ 冒烟通过（未写入任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
