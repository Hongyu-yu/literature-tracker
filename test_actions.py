#!/usr/bin/env python3
"""GitHub Actions 工作流不变量测试。

这些不变量是 2026-06 仓库优化批次固化下来的工程约束:
- 工作流 YAML 结构完整(name/on/jobs/runs-on/steps)
- checkout 一律浅克隆(fetch-depth: 1 或省略取默认 1)——本仓库 .git 数百 MB,
  代码已验证零处依赖 git 历史
- 所有向 main 推送的步骤必须带 rebase+重试循环(多工作流并发推送)
- 上传 Pages artifact 的 job 必须先从 data/ 复制 docs/data(docs/data 不入库)
- 每个 job 都有 timeout-minutes(防挂死耗满 6 小时配额)
- 定时任务时刻不重叠(避免周日 fetch 与 weekly 撞车)
"""
import glob
import os
import re

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = sorted(glob.glob(os.path.join(BASE_DIR, ".github", "workflows", "*.yml")))


def _load(path):
    with open(path, encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    # YAML 1.1 把裸 on: 解析为 True 键
    if True in wf and "on" not in wf:
        wf["on"] = wf.pop(True)
    return wf


def _jobs(wf):
    return (wf.get("jobs") or {}).items()


def _steps(job):
    return job.get("steps") or []


def test_workflows_well_formed():
    assert WORKFLOWS, "应当存在 workflow 文件"
    for path in WORKFLOWS:
        wf = _load(path)
        name = os.path.basename(path)
        assert wf.get("name"), f"{name} 缺 name"
        assert wf.get("on"), f"{name} 缺触发器"
        assert wf.get("jobs"), f"{name} 缺 jobs"
        for job_name, job in _jobs(wf):
            assert job.get("runs-on"), f"{name}:{job_name} 缺 runs-on"
            assert _steps(job), f"{name}:{job_name} 缺 steps"
            for i, step in enumerate(_steps(job)):
                assert "uses" in step or "run" in step, f"{name}:{job_name} step{i+1} 缺 uses/run"


def test_checkout_is_shallow():
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            for step in _steps(job):
                if "actions/checkout" in str(step.get("uses") or ""):
                    depth = (step.get("with") or {}).get("fetch-depth", 1)
                    if depth != 1:
                        bad.append(f"{os.path.basename(path)}:{job_name} fetch-depth={depth}")
    assert not bad, f"checkout 必须浅克隆: {bad}"


def test_pushes_to_main_have_retry_loop():
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            for step in _steps(job):
                run = step.get("run") or ""
                if "git push origin main" in run:
                    if "for i in 1 2 3 4 5" not in run or "git rebase" not in run:
                        bad.append(f"{os.path.basename(path)}:{job_name}")
    assert not bad, f"push 步骤缺 rebase+重试循环: {bad}"


def test_rebase_never_resolves_conflicts_against_the_job_output():
    """rebase 的 -X ours 会把冲突判给 origin/main，丢弃本次跑出来的日报数据。

    rebase 期间两边是对调的：ours = 被 replay 到的上游，theirs = 本次生成的提交。
    这些 workflow 花几十分钟到几小时产出内容，冲突时必须让自己的产物胜出（-X theirs）。
    test_pushes_to_main_have_retry_loop 只断言循环存在，抓不到方向写反。
    """
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            for step in _steps(job):
                for line in (step.get("run") or "").splitlines():
                    if "git rebase" in line and "-X ours" in line:
                        bad.append(f"{os.path.basename(path)}:{job_name}")
    assert not bad, f"git rebase 不得用 -X ours(会丢弃本次生成的内容)，应为 -X theirs: {bad}"


def test_ai_workflows_configure_gateway_hosts_fallback():
    """凡是调 AI 网关的 workflow 都要有 /etc/hosts 兜底（见 ccc88ff8a）。

    backfill-daily / backfill-weekly 曾漏掉这步，是最容易在 dispatch 时挂掉的两个。
    """
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            steps = _steps(job)
            uses_ai = any("AI_API_KEY" in (step.get("env") or {}) for step in steps)
            if not uses_ai:
                continue
            if not any("/etc/hosts" in (step.get("run") or "") for step in steps):
                bad.append(f"{os.path.basename(path)}:{job_name}")
    assert not bad, f"调用 AI 的 job 缺网关 hosts 兜底步骤: {bad}"


def test_dispatch_inputs_are_not_interpolated_into_scripts():
    """workflow_dispatch 输入必须经 env 传入，不能直接插进 run: 脚本体。

    ${{ inputs.* }} 会在 shell/Python 看到之前就被表达式引擎替换，等于让有写权限的
    dispatch 者执行任意代码，可读到该 job 的全部 secrets。
    """
    bad = []
    pattern = re.compile(r"\$\{\{\s*(?:github\.event\.)?inputs\.")
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            for step in _steps(job):
                if pattern.search(step.get("run") or ""):
                    bad.append(f"{os.path.basename(path)}:{job_name}:{step.get('name') or '?'}")
    assert not bad, f"dispatch 输入不得直接插进 run: 脚本，应经 env 传入: {bad}"


def test_pages_upload_jobs_prepare_docs_data():
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            steps = _steps(job)
            uploads = [i for i, s in enumerate(steps) if "upload-pages-artifact" in str(s.get("uses") or "")]
            if not uploads:
                continue
            prep = [i for i, s in enumerate(steps) if "cp -r data/* docs/data/" in (s.get("run") or "")]
            if not prep or min(prep) > min(uploads):
                bad.append(f"{os.path.basename(path)}:{job_name}")
    assert not bad, f"Pages 上传前必须从 data/ 复制 docs/data: {bad}"


def test_every_job_has_timeout():
    bad = []
    for path in WORKFLOWS:
        wf = _load(path)
        for job_name, job in _jobs(wf):
            if "timeout-minutes" not in job:
                bad.append(f"{os.path.basename(path)}:{job_name}")
    assert not bad, f"job 缺 timeout-minutes: {bad}"


def test_scheduled_crons_do_not_collide():
    slots = {}
    for path in WORKFLOWS:
        wf = _load(path)
        for entry in (wf.get("on") or {}).get("schedule") or []:
            cron = entry.get("cron") or ""
            m = re.match(r"^(\S+)\s+(\S+)\s", cron)
            assert m, f"{os.path.basename(path)} cron 不可解析: {cron}"
            minute, hours = m.group(1), m.group(2)
            for h in hours.split(","):
                key = (minute, h)
                if key in slots:
                    raise AssertionError(
                        f"cron 撞车 {key}: {slots[key]} 与 {os.path.basename(path)}")
                slots[key] = os.path.basename(path)


if __name__ == "__main__":
    for fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[fn]()
        print(f"✓ {fn}")
    print("OK")
