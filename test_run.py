#!/usr/bin/env python3
"""快速测试脚本 - 只用5个RSS源

注意：这是一个**脚本**，不是测试模块 —— 它会打真实的 RSS 外网并写入 data/ 与 articles/。
模块体必须留在 main() 里、由 __main__ 守卫保护：run_tests.py 会 import 所有 test_*.py，
若在 import 期就执行，每次 push/PR 的 smoke 都会去抓线上 RSS 并重写 data/index.json。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    # 输出到文件
    log_file = open("test_output.txt", "w", encoding="utf-8")

    def log(msg):
        print(msg)
        log_file.write(msg + "\n")
        log_file.flush()

    try:
        from rss_fetcher import RSSFetcher
        from translator import translate_text
        from data_manager import DataManager

        # 只用5个RSS源测试
        TEST_FEEDS = [
            "https://www.nature.com/nphys.rss",
            "https://www.nature.com/natmachintell.rss",
            "https://www.nature.com/ncomms.rss",
            "http://feeds.aps.org/rss/recent/prl.xml",
            "http://feeds.aps.org/rss/recent/prb.xml",
        ]

        KEYWORDS = ["ferro", "machine", "learning", "magne", "neural", "network", "potential", "hamiltonian"]

        log("🧪 快速测试 - 5个RSS源")

        fetcher = RSSFetcher(KEYWORDS)
        data_manager = DataManager("data", "articles")

        # 抓取
        log("📡 抓取RSS...")
        all_articles = fetcher.fetch_all(TEST_FEEDS)
        log(f"共获取 {len(all_articles)} 篇")

        # 筛选
        filtered = fetcher.filter_by_keywords(all_articles)
        log(f"🔍 筛选后 {len(filtered)} 篇")

        if not filtered:
            log("没有符合关键词的文献")
        else:
            # 只翻译前30篇测试
            test_articles = filtered[:30]
            log(f"🌐 翻译前 {len(test_articles)} 篇...")

            for i, article in enumerate(test_articles, 1):
                log(f"  [{i}] {article.title[:60]}...")
                article.title_zh = translate_text(article.title)
                if article.abstract:
                    article.abstract_zh = translate_text(article.abstract[:500])
                log(f"      → {article.title_zh[:60]}...")

            # 保存
            log("💾 保存数据...")
            data_manager.add_articles(test_articles)

            for article in test_articles:
                filepath = data_manager.save_article_markdown(article)
                log(f"  已保存: {filepath}")

            data_manager.generate_index_json()

        log("✅ 测试完成!")
        return 0

    except Exception as e:
        log(f"❌ 错误: {e}")
        import traceback
        log(traceback.format_exc())
        return 1

    finally:
        log_file.close()


if __name__ == "__main__":
    sys.exit(main())
