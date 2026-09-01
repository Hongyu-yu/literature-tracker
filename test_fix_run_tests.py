#!/usr/bin/env python3
"""run_tests.py 自身的回归测试(跑测试的东西也得有人测)。

两个已复现的坑:

1) 每条用例的守卫是 `except Exception`,而 SystemExit 属于 BaseException。
   测试里只要有一句 sys.exit()(最常见的是调某模块 main() 却忘了 patch sys.argv,
   argparse 遇到未知参数就 sys.exit(2)),异常会一路穿出 main() 和
   `sys.exit(main(sys.argv[1:]))`:后面的测试模块一个都不跑,汇总行和 FAILURES 块
   一个字都不打印。若退出码恰好是 0(success-path 的 main()),CI 会「全绿」通过,
   实际只跑了一小半用例。模块顶层的 sys.exit() 同理(import 处的守卫一样窄)。

2) ModuleNotFoundError 降级成 skip 的条件只看「仓库根目录有没有同名 .py」,
   于是一方模块被删/改名后,整个测试模块被静默算作 skip;而 skip 从不影响退出码,
   ~250 行 ✓ 里那一行 ⊘ 没人看得见。

修复后:两处守卫改成 `except BaseException`(KeyboardInterrupt 照旧透传),
降级 skip 只认 requirements.txt 里的第三方包白名单,skip 汇总单独打一块,
并新增 --strict(给 CI 用:任何 skip 都算失败)。
"""

import os
import subprocess
import sys
import tempfile

import run_tests

REPO = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(REPO, "run_tests.py")


def _run(tmpdir, files, args=()):
    """把探针模块写进临时目录,用子进程跑 run_tests.py,返回 (退出码, 全部输出)。

    必须用子进程:要验证的正是「SystemExit 把整个进程带走」这件事,在进程内跑就看不见。
    """
    for fname, body in files.items():
        with open(os.path.join(tmpdir, fname), "w", encoding="utf-8") as fh:
            fh.write(body)
    env = dict(os.environ)
    env["PYTHONPATH"] = tmpdir          # 探针模块不在仓库里,得让子进程找得到
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, RUNNER, *args, *sorted(files)],
        cwd=tmpdir, env=env, capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_sysexit_inside_a_test_is_recorded_not_fatal():
    """用例里的 sys.exit(0) 只能算一条失败,不能截断整轮运行。"""
    with tempfile.TemporaryDirectory() as td:
        rc, out = _run(td, {
            "zz_probe_exit.py": "import sys\n\n\ndef test_calls_sys_exit():\n    sys.exit(0)\n",
            "zz_probe_next.py": "def test_runs_anyway():\n    pass\n",
        })
    assert "zz_probe_next.test_runs_anyway" in out, out   # 后面的模块必须继续执行
    assert "1 passed, 1 failed, 0 skipped" in out, out    # 汇总行必须打印出来
    assert "=== FAILURES ===" in out, out
    assert rc == 1, f"rc={rc}\n{out}"


def test_sysexit_at_import_time_is_recorded_not_fatal():
    """模块顶层的 sys.exit(3) 同样只能算一条 import 失败。"""
    with tempfile.TemporaryDirectory() as td:
        rc, out = _run(td, {
            "zz_probe_aexit.py": "import sys\n\nsys.exit(3)\n",
            "zz_probe_bnext.py": "def test_runs_anyway():\n    pass\n",
        })
    assert "zz_probe_bnext.test_runs_anyway" in out, out
    assert "1 passed, 1 failed, 0 skipped" in out, out
    assert "import error" in out, out
    assert rc == 1, f"rc={rc}\n{out}"


def test_missing_first_party_module_fails_instead_of_silent_skip():
    """一方模块被删/改名 → 硬失败;绝不能当成「本机缺依赖」静默跳过。"""
    with tempfile.TemporaryDirectory() as td:
        rc, out = _run(td, {
            "zz_probe_gone.py":
                "import zz_module_that_was_deleted\n\n\ndef test_never_runs():\n    pass\n",
        })
    assert "0 passed, 1 failed, 0 skipped" in out, out
    assert rc == 1, f"rc={rc}\n{out}"


def test_missing_third_party_dep_still_degrades_to_skip():
    """fail-soft 不能被误伤:requirements.txt 里的依赖本机没装,仍然只 skip、退出码 0。

    直接 raise 而不是真去 import,是因为 CI 里这些依赖都装好了,靠「没装」测不出来。
    """
    body = ("raise ModuleNotFoundError(\"No module named 'deep_translator'\",\n"
            "                          name='deep_translator')\n")
    with tempfile.TemporaryDirectory() as td:
        rc, out = _run(td, {"zz_probe_dep.py": body})
    assert "missing optional dep: deep_translator" in out, out
    assert "0 passed, 0 failed, 1 skipped" in out, out
    assert "=== SKIPPED ===" in out, out   # skip 必须在汇总里再露一次脸
    assert rc == 0, f"rc={rc}\n{out}"


def test_strict_mode_turns_skips_into_failure():
    """默认仍然 fail-soft(本地无 pip);--strict 下任何 skip 都算失败(给 CI 用)。"""
    files = {"zz_probe_fixture.py": "def test_needs_pytest_fixture(tmp_path):\n    pass\n"}
    with tempfile.TemporaryDirectory() as td:
        rc_soft, out_soft = _run(td, files)
    with tempfile.TemporaryDirectory() as td:
        rc_strict, out_strict = _run(td, files, args=("--strict",))
    assert "0 passed, 0 failed, 1 skipped" in out_soft, out_soft
    assert rc_soft == 0, f"rc={rc_soft}\n{out_soft}"
    assert "0 passed, 0 failed, 1 skipped" in out_strict, out_strict
    assert rc_strict == 1, f"rc={rc_strict}\n{out_strict}"


def test_unknown_flag_is_reported_not_treated_as_a_module():
    """打错的开关要报出来,不能被当成模块名默默跳过。"""
    with tempfile.TemporaryDirectory() as td:
        rc, out = _run(td, {"zz_probe_ok.py": "def test_ok():\n    pass\n"}, args=("--bogus",))
    assert "未知参数" in out, out
    assert rc == 2, f"rc={rc}\n{out}"


def test_is_optional_dep_only_allows_declared_third_party():
    assert run_tests._is_optional_dep("deep_translator", REPO) is True
    assert run_tests._is_optional_dep("json_repair", REPO) is True
    # 不在白名单里(例如被删掉的一方模块)→ 必须暴露成失败
    assert run_tests._is_optional_dep("zz_module_that_was_deleted", REPO) is False
    assert run_tests._is_optional_dep("", REPO) is False
    assert run_tests._is_optional_dep(None, REPO) is False
    # 名字虽在白名单里,但同目录下有同名 .py → 按一方模块处理
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "requests.py"), "w", encoding="utf-8") as fh:
            fh.write("")
        assert run_tests._is_optional_dep("requests", td) is False


def test_detail_keeps_exception_message_and_names_systemexit():
    # 普通异常保持原样(输出格式不变)
    assert run_tests._detail(ValueError("boom")) == "boom"
    # SystemExit(0) 直接 str() 是 "0",看不出发生了什么,必须带上类名
    assert run_tests._detail(SystemExit(0)) == "SystemExit: 0"


def test_third_party_whitelist_covers_every_import_in_repo():
    """白名单是手写的,加了新依赖忘了同步 → 本机会从 skip 变成硬失败。这里提前拦住。"""
    import re
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    pattern = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)")
    unknown = set()
    for fname in sorted(os.listdir(REPO)):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(REPO, fname), encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = pattern.match(line)
                if not m:
                    continue
                top = m.group(1)
                if top in stdlib or top in run_tests.THIRD_PARTY_MODULES:
                    continue
                if os.path.exists(os.path.join(REPO, top + ".py")):
                    continue          # 一方模块
                unknown.add(top)
    assert not unknown, f"这些顶层 import 既不是标准库也不是一方模块,请补进 run_tests.THIRD_PARTY_MODULES: {sorted(unknown)}"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] run_tests 守卫/白名单/--strict 回归测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
