#!/usr/bin/env python3
"""generate-deep.yml 的两条时序不变量（都属于"静默丢数据"，CI 全绿也看不出来）。

1) 目标日必须在 job 开头钉死一次，两次 generate_daily_pages.py 共用。
   不钉的话两次调用各自算 beijing_yesterday()，而它们相隔数小时（深读+海报）；跨过北京零点
   (16:00 UTC) 后，第一步写的是 D-1 的 daily_summary sidecar，末尾发信那步却去找 D 的
   sidecar —— 没人生成过，打印"⏭️ 每日邮件跳过"，D-1 的日报邮件永久丢失（次日跑 D，没人回头
   补 D-1）。实测 08-27 16:47 UTC / 08-28 16:55 UTC 两轮深读就是这么收工的。

2) 本 job 不得比 fetch.yml 的夜间那轮更早起跑，且开工前要再对齐一次 origin/main。
   fetch cron 00:00 UTC + timeout 240 分钟 ⇒ 最晚 04:00 UTC 推完（实测 03:34/03:55/04:14/
   06:26）。原来 cron 是 03:30 UTC，checkout 拿到的 index.json/ai_relevant.json 里还没有当晚
   抓取结果；基于陈旧语料重算出的日报页面/sidecar，随后被推送循环的 `-X theirs` 判赢，覆盖掉
   fetch 刚推上去的新页面，邮件更是发一次就写死 email_sent.json，当天再也补发不了。

用纯标准库做文本断言：本仓 CI 才装 PyYAML，run_tests.py 在本机会把 import yaml 的模块整体
跳过（见 test_actions.py 的下场），那样这两条不变量本地就等于没测。
"""

import os
import re
import subprocess
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEEP_YML = os.path.join(BASE_DIR, ".github", "workflows", "generate-deep.yml")
FETCH_YML = os.path.join(BASE_DIR, ".github", "workflows", "fetch.yml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _run_block(text, step_name):
    """取出某个 step 的 `run: |` 块正文（已去掉缩进）。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("- name:") and step_name in ln:
            start = i
            break
    assert start is not None, f"generate-deep.yml 缺少步骤: {step_name}"
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("- name:") or stripped.startswith("- uses:"):
            break
        if stripped == "run: |":
            indent = len(lines[j]) - len(lines[j].lstrip())
            body = []
            for k in range(j + 1, len(lines)):
                if not lines[k].strip():
                    body.append("")
                    continue
                if len(lines[k]) - len(lines[k].lstrip()) <= indent:
                    break
                body.append(lines[k][indent + 2:])
            return "\n".join(body)
    raise AssertionError(f"步骤 {step_name} 没有 `run: |` 块")


def _daily_page_runs(text):
    """所有调用 generate_daily_pages.py 的 run: 行（跳过注释里提到脚本名的行）。"""
    return [ln.strip() for ln in text.splitlines() if "generate_daily_pages.py" in ln
            and ln.strip().startswith(("run:", "python "))]


def _first_daily_run_lineno(text):
    for i, ln in enumerate(text.splitlines()):
        if "generate_daily_pages.py" in ln and ln.strip().startswith(("run:", "python ")):
            return i
    raise AssertionError("generate-deep.yml 没有调用 generate_daily_pages.py")


def _first_lineno(text, needle, predicate=None):
    for i, ln in enumerate(text.splitlines()):
        if needle in ln and (predicate is None or predicate(ln)):
            return i
    return -1


def _cron_start_minutes(text):
    """workflow 的每个 cron 触发时刻（当天第几分钟，UTC）。"""
    out = []
    for cron in re.findall(r"^\s*-\s*cron:\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", text, re.M):
        parts = cron.split()
        assert len(parts) == 5, f"cron 不可解析: {cron}"
        minute, hours = parts[0], parts[1]
        assert minute.isdigit(), f"本测试只支持定点分钟的 cron: {cron}"
        for h in hours.split(","):
            assert h.isdigit(), f"本测试只支持定点小时的 cron: {cron}"
            out.append(int(h) * 60 + int(minute))
    return sorted(out)


def _first_timeout(text):
    m = re.search(r"^\s*timeout-minutes:\s*(\d+)\s*$", text, re.M)
    assert m, "缺少 timeout-minutes"
    return int(m.group(1))


def test_both_daily_page_invocations_share_one_pinned_target_day():
    """两次 generate_daily_pages.py 必须都吃同一个 $DAILY_DATE，且 pin 步骤排在它们之前。"""
    text = _read(DEEP_YML)
    runs = _daily_page_runs(text)
    assert len(runs) == 2, f"预期 generate-deep 里有两次日报调用，实际 {len(runs)}: {runs}"
    for line in runs:
        assert '--date "$DAILY_DATE"' in line, f"日报调用未钉死目标日（跨零点会丢一天邮件）: {line}"

    pin_at = _first_lineno(text, 'echo "DAILY_DATE=')
    assert pin_at >= 0, "缺少把 DAILY_DATE 写进 $GITHUB_ENV 的步骤"
    assert pin_at < _first_daily_run_lineno(text), \
        "钉目标日的步骤必须排在第一次 generate_daily_pages.py 之前"

    # 发信入口的连续子串是 test_daily_email.py 的断言对象：--date 只能追加在末尾。
    assert "--rerender-only --days 4 --send-email" in text, \
        "--date 必须追加在末尾，不能插进 test_daily_email.py 断言的那段连续子串里"


def test_pin_step_yields_beijing_yesterday():
    """pin 步骤写进 $GITHUB_ENV 的就是北京时间的昨天。"""
    script = _run_block(_read(DEEP_YML), "Pin target day")
    tmp = tempfile.mkdtemp()
    env_file = os.path.join(tmp, "github_env")
    open(env_file, "w").close()
    env = dict(os.environ, GITHUB_ENV=env_file)
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"pin 步骤失败: {proc.stderr}"

    expected = subprocess.run(["bash", "-c", "TZ=Asia/Shanghai date -d yesterday +%F"],
                              capture_output=True, text=True).stdout.strip()
    with open(env_file, encoding="utf-8") as f:
        written = f.read().strip()
    assert written == f"DAILY_DATE={expected}", f"期望 DAILY_DATE={expected}，实际 {written!r}"


def test_pin_step_degrades_to_script_default_instead_of_crashing():
    """date 挂掉时留空而不是写进垃圾：脚本里 `args.date or beijing_yesterday()` 会退回原行为。

    fail-soft 约束——一个取时间的小步骤不该让整轮深读（数小时 AI 产出）直接没跑成。
    """
    script = _run_block(_read(DEEP_YML), "Pin target day")
    tmp = tempfile.mkdtemp()
    stub_dir = os.path.join(tmp, "bin")
    os.makedirs(stub_dir)
    stub = os.path.join(stub_dir, "date")
    with open(stub, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho 'date: boom' >&2\nexit 1\n")
    os.chmod(stub, 0o755)
    env_file = os.path.join(tmp, "github_env")
    open(env_file, "w").close()
    env = dict(os.environ,
               PATH=stub_dir + os.pathsep + os.environ.get("PATH", ""),
               GITHUB_ENV=env_file)
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"date 失败不该让这步非零退出: rc={proc.returncode} {proc.stderr}"
    with open(env_file, encoding="utf-8") as f:
        written = f.read().strip()
    assert written == "DAILY_DATE=", f"date 失败时应留空回落，实际写入 {written!r}"


def test_syncs_to_latest_main_before_regenerating_daily_pages():
    """开工前重新对齐 origin/main，避免拿 fetch 尚未推上来的陈旧语料重算日报。"""
    text = _read(DEEP_YML)
    sync_at = _first_lineno(text, "git reset --hard origin/main")
    assert sync_at >= 0, "缺少开工前对齐 origin/main 的步骤（会基于陈旧 index.json 重算日报）"
    assert sync_at < _first_daily_run_lineno(text), \
        "对齐 origin/main 必须发生在第一次 generate_daily_pages.py 之前"

    block = _run_block(text, "Sync to latest main")
    assert "git fetch origin main" in block, "reset 之前要先 fetch"
    # fail-soft：网络抖动不能干掉整轮深读
    assert block.count("exit 0") >= 2, "fetch/reset 失败都应降级继续（exit 0），不得中断本 job"


def test_cron_starts_after_the_nightly_fetch_run_can_finish():
    """深读起跑时刻必须晚于 fetch 夜间那轮的最晚收工时刻，且要在 fetch 下一轮之前跑完。"""
    fetch_text = _read(FETCH_YML)
    deep_text = _read(DEEP_YML)

    fetch_starts = _cron_start_minutes(fetch_text)
    assert fetch_starts, "fetch.yml 缺 cron"
    fetch_deadline = fetch_starts[0] + _first_timeout(fetch_text)

    deep_starts = _cron_start_minutes(deep_text)
    assert len(deep_starts) == 1, f"预期 generate-deep 只有一个 cron: {deep_starts}"
    deep_start = deep_starts[0]
    deep_timeout = _first_timeout(deep_text)

    assert deep_start >= fetch_deadline, (
        f"generate-deep 起跑({deep_start} 分)早于 fetch 夜间轮的最晚收工({fetch_deadline} 分)，"
        "会 checkout 到缺当晚抓取结果的 main，重算的日报再被 -X theirs 盖过 fetch 的新页面")

    next_fetch = [s for s in fetch_starts if s > deep_start]
    if next_fetch:
        assert deep_start + deep_timeout <= next_fetch[0], (
            f"generate-deep({deep_start}+{deep_timeout} 分)会撞上 fetch 的下一轮({next_fetch[0]} 分)")


if __name__ == "__main__":
    for fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[fn]()
        print(f"✓ {fn}")
    print("OK")
