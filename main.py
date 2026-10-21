"""CLI 入口。

用法：
    python main.py                 # 按 config.yaml 跑（默认 dry_run，只答题不提交）
    python main.py --live          # 真正交卷
    python main.py --exam-id 123   # 指定考试
    python main.py --status        # 查看断点状态（不开考、不联网）
    python main.py --no-resume     # 忽略断点（检测到未完成记录会停止，不会重复开考）
    python main.py --debug         # 打印每个 HTTP 请求

退出码：
    0 正常；1 未处理错误；2 配置错误；3 会话/状态错误；
    4 登录失败/风控；5 考试窗口不满足；6 交卷未确认；130 用户中断。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# 允许 `python main.py` 直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config as cfgmod  # noqa: E402
from src.errors import AppError, ConfigError  # noqa: E402
from src.runner import EXIT_CONFIG, EXIT_ERROR, run  # noqa: E402
from src.session import SessionError  # noqa: E402
from src.state import RunLock, StateStore  # noqa: E402


def _setup_console() -> None:
    """Windows 控制台默认 GBK，直接输出 UTF-8 会乱码。

    必须切成 65001，并且把 Python 的 stdout/stderr 也设成 UTF-8 带 replace，
    否则遇到生僻字还会抛 UnicodeEncodeError。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _print_status(paths) -> int:
    st = StateStore(paths.state).load()
    if st is None:
        print(f"没有断点状态（{paths.state} 不存在）")
        return 0
    data = st.to_dict()
    data.pop("answers", None)
    data["answered_count"] = st.answer_count()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n账号     ：{paths.key}")
    print(f"状态文件 ：{paths.state}")
    return 0


def main() -> int:
    _setup_console()
    ap = argparse.ArgumentParser(description="yooc 考试自动化（硬化版）")
    ap.add_argument("--config", default=None, help="配置文件路径，默认 ./config.yaml")
    ap.add_argument(
        "--build-bank",
        default=None,
        metavar="TXT",
        help="把答案 TXT 转成题库 JSON 后退出（配合 --bank 指定输出路径）",
    )
    ap.add_argument(
        "--bank",
        default=None,
        help="题库 JSON 路径，覆盖 config.yaml 的 bank.path（与 --build-bank 同用时是输出路径）",
    )
    ap.add_argument("--bank-name", default=None, help="题库名称（写入 JSON，便于识别）")
    ap.add_argument("--no-bank", action="store_true", help="本次运行不使用本地题库")
    ap.add_argument(
        "--llm-first",
        action="store_true",
        help="先让模型把整卷判一遍（推理/搜索都会跑），交卷前再用题库校正",
    )
    ap.add_argument(
        "--no-answer-key",
        action="store_true",
        help="忽略平台 paper 里的答案（某些考试返回的是占位数据）",
    )
    ap.add_argument("--live", action="store_true", help="关闭 dry_run，真正交卷")
    ap.add_argument("--dry-run", action="store_true", help="强制只答题不提交")
    ap.add_argument(
        "--start-only",
        action="store_true",
        help="只开考并答题，不交卷；之后用 --live 重跑即可续跑同一份记录并提交",
    )
    ap.add_argument(
        "--start-dry-run",
        action="store_true",
        help="演练时也真的开考取卷（会占用本次作答机会，仅确认要练流程时用）",
    )
    ap.add_argument("--exam-id", default=None, help="指定 examId")
    ap.add_argument("--keyword", default=None, help="按考试标题关键字选择")
    ap.add_argument("--headless", action="store_true", help="无头运行（需已登录过）")
    ap.add_argument("--reset-login", action="store_true", help="删除已保存的登录态，重新登录")
    ap.add_argument("--no-resume", action="store_true", help="检测到未完成作答记录时停止（不续跑）")
    ap.add_argument(
        "--reset-state",
        action="store_true",
        help="删除本地断点状态文件（可能造成重复开考，需同时加 --yes）",
    )
    ap.add_argument("--yes", action="store_true", help="确认危险操作")
    ap.add_argument("--status", action="store_true", help="打印断点状态后退出（不联网）")
    ap.add_argument("--login-only", action="store_true", help="只登录并验证凭据，不开考")
    ap.add_argument("--wait", type=int, default=600, help="等待手动登录的秒数，默认 600")
    ap.add_argument("--user", default=None, help="账号（手机号），覆盖 config")
    ap.add_argument("--pass", dest="pwd", default=None, help="密码，覆盖 config")
    ap.add_argument(
        "--login-mode",
        choices=["auto", "password", "browser"],
        default=None,
        help="登录方式：password=纯接口(快) / browser=开浏览器 / auto=先接口后浏览器",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="跳过开考时间/截止时间检查（会消耗考试机会，慎用）",
    )
    ap.add_argument(
        "--auto-start",
        action="store_true",
        help="到点自动开考：开考时间没到就等，到点自己开考（不消耗额外请求）",
    )
    ap.add_argument(
        "--start-at",
        default=None,
        help="指定自动开考时刻，如 '2026-10-19 00:00:00' 或 '08:30'；"
        "需配合 --auto-start（会自动打开 auto_start）",
    )
    ap.add_argument(
        "--force-start",
        action="store_true",
        help="上次开考结果未知时，跳过服务端回查直接重新开考（可能重复消耗机会）",
    )
    args = ap.parse_args()

    # 账号隔离：--status / --reset-state / --reset-login 都要在完整加载配置前
    # 就知道当前账号落在哪个目录，所以先用轻量读取推导一次。
    paths = cfgmod.paths_for(
        cfgmod.peek_account_key(Path(args.config) if args.config else None)
    )

    if args.status:
        return _print_status(paths)

    if args.build_bank:
        from src.bank_builder import build_bank

        out = Path(args.bank) if args.bank else (cfgmod.ROOT / "data" / "question_bank.json")
        if not out.is_absolute():
            out = cfgmod.ROOT / out
        try:
            build_bank(args.build_bank, out, name=args.bank_name or "")
        except Exception as exc:  # noqa: BLE001
            print(f"[题库] 生成失败：{exc}")
            return EXIT_ERROR
        print(f"[题库] 之后运行时会自动加载：{out}")
        return 0

    if args.reset_state:
        if not args.yes:
            print(
                "[拒绝] --reset-state 会删除断点状态，可能导致下次重复开考（白扔机会）。\n"
                "  确认要删请再加 --yes。"
            )
            return EXIT_CONFIG
        lock = RunLock(paths.lock)
        if not lock.acquire():
            print("[拒绝] 有任务正在运行，不能删除状态文件。请先等它结束。")
            return EXIT_CONFIG
        try:
            paths.state.unlink()
            print(f"[ok] 已删除状态文件：{paths.state}")
        except FileNotFoundError:
            print("状态文件本来就不存在")
        except OSError as exc:
            print(f"[错误] 删除失败：{exc}")
            return EXIT_ERROR
        finally:
            lock.release()

    try:
        cfg = cfgmod.load(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}")
        return EXIT_CONFIG

    if args.live and args.dry_run:
        print("--live 与 --dry-run 冲突")
        return 2
    if args.live and args.start_only:
        print("--live 与 --start-only 冲突")
        return 2
    if args.start_only and (args.dry_run or args.start_dry_run):
        print("--start-only 与 --dry-run / --start-dry-run 冲突")
        return 2
    if args.live and args.start_dry_run:
        print("--live 与 --start-dry-run 冲突")
        return 2
    if args.live:
        cfg.exam.dry_run = False
        cfg.exam.auto_submit = True
    if args.start_only:
        cfg.exam.dry_run = False
        cfg.exam.auto_submit = False
        print("[开始考试] 开考 + 答题，不交卷；之后用 --live 重跑提交")
    if args.dry_run:
        cfg.exam.dry_run = True
    if args.start_dry_run:
        cfg.exam.dry_run = True
        cfg.exam.allow_start_in_dry_run = True
        print(
            "[注意] --start-dry-run：演练会真的调用 start/action 并取卷，\n"
            "       本次作答机会会被占用；之后用 --live 可在同一次作答记录上交卷。"
        )
    if args.exam_id:
        cfg.exam.exam_id = args.exam_id
    if args.auto_start or args.start_at:
        cfg.exam.auto_start = True
        print("[自动开考] 已开启：没到开考时间会原地等待，到点自动调用 start/action")
    if args.start_at:
        cfg.exam.start_at = args.start_at
    if args.keyword:
        cfg.exam.exam_name_keyword = args.keyword
    if args.headless:
        cfg.account.headless = True
    if args.reset_login:
        lock = RunLock(paths.lock)
        if not lock.acquire():
            print("[拒绝] 有任务正在运行，不能删除登录态。请先等它结束。")
            return EXIT_CONFIG
        try:
            paths.session.unlink()
            print(f"[ok] 已删除登录态：{paths.session}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[warn] 删除登录态失败：{exc}")
        finally:
            lock.release()
    if args.no_resume:
        cfg.exam.resume = False
    if args.no_bank:
        cfg.bank.enabled = False
    if args.llm_first:
        cfg.bank.mode = "llm_first"
        cfg.bank.enabled = True
    if args.no_answer_key:
        cfg.exam.use_answer_key = False
    if args.bank:
        cfg.bank.path = Path(args.bank).expanduser()
        if not cfg.bank.path.is_absolute():
            cfg.bank.path = cfgmod.ROOT / cfg.bank.path
        if not cfg.bank.path.exists():
            print(f"[warn] 题库文件不存在：{cfg.bank.path}（本次将跳过题库命中）")
    if args.login_mode:
        cfg.account.login_mode = args.login_mode
    if args.user:
        cfg.account.username = args.user
    if args.pwd:
        cfg.account.password = args.pwd

    try:
        return run(
            cfg,
            login_only=args.login_only,
            wait_seconds=args.wait,
            user=args.user,
            pwd=args.pwd,
            force=args.force,
            force_start=args.force_start,
        )
    except SessionError as exc:
        print(f"\n[会话错误] {exc}")
        return 3
    except KeyboardInterrupt:
        print("\n已中断（作答状态已保存，重跑会自动续跑）")
        return 130
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}")
        return EXIT_CONFIG
    except AppError as exc:
        print(f"\n[错误] {exc}")
        return EXIT_ERROR
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        print("\n[未处理异常]")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    _code = 0
    try:
        _code = main()
    except SystemExit as e:
        # SystemExit 的字符串消息原本由解释器打印；这里捕获后要自己打，否则会静默吞掉。
        if isinstance(e.code, str):
            print(e.code, flush=True)
            _code = 1
        else:
            _code = int(e.code or 0)
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        _code = 1
    finally:
        # 关键：放在 finally 里。之前 main() 抛 SystemExit 会跳过下面的代码，
        # 双击运行时窗口瞬间关闭 —— 这就是"闪退"的真正原因。
        if getattr(sys, "frozen", False) and len(sys.argv) == 1:
            try:
                input("\n按回车键退出…")
            except Exception:
                pass
    raise SystemExit(_code)
