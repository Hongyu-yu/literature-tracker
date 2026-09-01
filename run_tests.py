#!/usr/bin/env python3
"""Stdlib-only test runner (no pytest needed locally).

Discovers test_*.py modules and runs every top-level `test_*` function that
takes no required arguments. Tests MUST avoid pytest fixtures (monkeypatch,
tmp_path); use unittest.mock + tempfile instead so they run both here and
under pytest in CI.

Usage:
  python3 run_tests.py                 # run all test_*.py
  python3 run_tests.py test_foo.py ... # run specific modules
  python3 run_tests.py --strict        # CI 用:任何 skip 也算失败
"""
import sys, os, glob, importlib, inspect, traceback

# requirements.txt 里第三方包对应的顶层 import 名(dist 名 → import 名不是机械映射,
# 故写死在这里)。只有这些名字缺失才允许把 ModuleNotFoundError 降级为 skip;
# 其它缺失(尤其是本仓库自己的模块被删/改名)一律按失败处理 —— 否则整个测试模块
# 会被静默跳过,CI 依旧全绿。新增第三方依赖时记得同步这里。
THIRD_PARTY_MODULES = {
    "feedparser", "requests", "bs4", "deep_translator", "dateutil",
    "schedule", "json_repair", "PIL", "pytest", "pdfminer", "yaml",
}

def _is_optional_dep(missing, here):
    """判断 ModuleNotFoundError 是否属于「本机没装的第三方依赖」(可降级为 skip)。"""
    if not missing or missing not in THIRD_PARTY_MODULES:
        return False
    # 仓库根目录有同名 .py 时说明是一方模块,导入失败必须暴露
    return not os.path.exists(os.path.join(here, missing + ".py"))

def _detail(e):
    """SystemExit(0) 这类非 Exception 直接 str() 会打出空信息,补上类名。"""
    return str(e) if isinstance(e, Exception) else f"{type(e).__name__}: {e}"

def _runnable(fn):
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    return all(p.default is not inspect.Parameter.empty
               or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               for p in sig.parameters.values())

def main(argv):
    strict = "--strict" in argv
    unknown_flags = [a for a in argv if a.startswith("-") and a != "--strict"]
    if unknown_flags:
        print(f"⚠️ 未知参数:{' '.join(unknown_flags)}(仅支持 --strict)")
        return 2
    mods = [a for a in argv if not a.startswith("-")] or sorted(glob.glob("test_*.py"))
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    passed = failed = skipped = 0
    failures = []
    skips = []
    for path in mods:
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if _is_optional_dep(missing, here):
                # 第三方依赖本机未装(本地无 pip):降级为 skip,CI(装全依赖)仍全量执行
                skipped += 1
                msg = f"{name} (missing optional dep: {missing} — skipped locally)"
                skips.append(msg)
                print(f"⊘ {msg}")
                continue
            failures.append((name, "<import>", traceback.format_exc()))
            failed += 1
            hint = ""
            if missing and not os.path.exists(os.path.join(here, missing + ".py")):
                hint = f" (若 {missing} 是新增的第三方依赖,请加入 run_tests.THIRD_PARTY_MODULES)"
            print(f"✗ {name}: import error: {e}{hint}")
            continue
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            # 不能只 catch Exception:模块顶层的 sys.exit() 会一路穿出 main(),
            # 后面的测试模块全部不执行且不打印任何汇总(SystemExit 属于 BaseException)
            failures.append((name, "<import>", traceback.format_exc()))
            failed += 1
            print(f"✗ {name}: import error: {_detail(e)}")
            continue
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            if not _runnable(fn):
                skipped += 1
                msg = f"{name}.{attr} (needs fixture — skipped locally)"
                skips.append(msg)
                print(f"⊘ {msg}")
                continue
            try:
                fn()
                passed += 1
                print(f"✓ {name}.{attr}")
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                # 同上:测试里一句 sys.exit() 会截断整轮运行(哪怕 exit code 是 0,
                # CI 会以「全绿」结束却只跑了一小半),必须记成普通失败
                failed += 1
                failures.append((name, attr, traceback.format_exc()))
                print(f"✗ {name}.{attr}: {_detail(e)}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if skips:
        # skip 行淹没在几百行 ✓ 里没人看得见,汇总时再打一遍
        print("\n=== SKIPPED ===")
        for msg in skips:
            print(f"  ⊘ {msg}")
    if failures:
        print("\n=== FAILURES ===")
        for name, attr, tb in failures:
            print(f"\n--- {name}.{attr} ---\n{tb}")
    if strict and skipped:
        print(f"\n⚠️ --strict:{skipped} 个测试被跳过,按失败处理")
        return 1
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
