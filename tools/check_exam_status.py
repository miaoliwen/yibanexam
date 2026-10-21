# -*- coding: utf-8 -*-
"""只读查询：某个账号的考试完成情况。

不调用任何写接口（不 start / 不 submit），只做：
    exam_list -> exam_detail -> exam_result

用法：
    python tools/check_exam_status.py --config config.account-b.yaml
    python tools/check_exam_status.py --config xxx.yaml --dump   # 只落原始 JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as cfgmod  # noqa: E402
from src.api import YoocApi  # noqa: E402
from src.login import load_saved  # noqa: E402


def _num(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def _items(payload: Any) -> list[dict]:
    """从 exam_list 返回里挖出考试条目（平台结构多层嵌套，逐层兜底）。"""
    out: list[dict] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for it in node:
                if isinstance(it, dict) and (
                    "examId" in it or "id" in it and "name" in it
                ):
                    out.append(it)
                elif isinstance(it, (dict, list)):
                    walk(it, depth + 1)
        elif isinstance(node, dict):
            for key in ("data", "result", "list", "items", "exams", "rows"):
                if key in node:
                    walk(node[key], depth + 1)

    walk(payload)
    # 去重（同一 examId 只留一条）
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in out:
        eid = str(it.get("examId") or it.get("id") or "")
        if eid in seen:
            continue
        seen.add(eid)
        uniq.append(it)
    return uniq


def _pick(d: dict, *names: str) -> Any:
    for n in names:
        if n in d and d[n] not in (None, "", []):
            return d[n]
    return None


def _verdict(exam: dict, detail: Any, result: Any) -> tuple[str, list[str]]:
    """给出「是否完成」的结论 + 依据。"""
    ev: list[str] = []
    eid = _pick(exam, "examId", "id")
    eu = _pick(exam, "examuserId", "examUserId")
    status = _pick(exam, "status", "examStatus", "state")
    score = _pick(exam, "score", "userScore")

    ev.append(f"examuserId={eu}")
    if status is not None:
        ev.append(f"status={status}")
    if score is not None:
        ev.append(f"score={score}")

    result_err = ""
    if isinstance(result, dict) and result.get("error"):
        result_err = str(result["error"])

    if result_err and "未参加" in result_err:
        return "未参加考试（未开考，未完成）", ev + [f"result: {result_err}"]
    if str(eu) in ("0", "None", ""):
        return "未参加考试（未开考，未完成）", ev
    if str(status) in ("4", "5"):
        return "已完成（服务端状态为已完成/已结束）", ev
    if str(status) == "3":
        return "已开考、未交卷（作答中或中断）", ev
    return "已开考但状态不明，需人工核对", ev


def _describe(exam: dict, detail: Any, result: Any) -> str:
    eid = _pick(exam, "examId", "id")
    title = _pick(exam, "name", "title", "examName") or "(无标题)"
    start = _pick(exam, "started", "startTime", "start_time", "beginTime")
    end = _pick(exam, "expired", "endTime", "end_time")
    verdict, ev = _verdict(exam, detail, result)

    lines = [f"- examId={eid}  {title}"]
    if start or end:
        lines.append(f"    时间窗  : {start} ~ {end}")
    lines.append(f"    证据    : {', '.join(ev)}")

    if isinstance(detail, dict):
        d = detail.get("data") if isinstance(detail.get("data"), dict) else detail
        if d.get("status") is not None and str(d.get("status")) != str(_pick(exam, "status")):
            lines.append(f"    详情状态: {d.get('status')}（列表状态为 {_pick(exam, 'status')}）")

    lines.append(f"    结论    : {verdict}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dump", action="store_true", help="只落原始 JSON，不解析")
    args = ap.parse_args()

    cfg = cfgmod.load(Path(args.config))
    print(f"账号     : {cfg.account.profile_key}")
    print(f"目录     : {cfg.paths.root}")
    print(f"分组     : {cfg.account.group_id}")

    creds = load_saved(cfg)
    if not creds:
        print("[错误] 没有可用登录态，请先 --login-only")
        return 2
    print(f"userId   : {creds.get('user_id')}")

    api = YoocApi(cfg, creds)
    raw = api.exam_list()
    cfg.paths.root.mkdir(parents=True, exist_ok=True)
    (cfg.paths.root / "exam_list_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    items = _items(raw)
    print(f"考试条目 : {len(items)}")
    if args.dump or not items:
        print(f"原始返回 : {cfg.paths.root / 'exam_list_raw.json'}")
        if not items:
            print("\n--- 原始返回预览 ---")
            print(json.dumps(raw, ensure_ascii=False, indent=2)[:3000])
        return 0

    print()
    for e in items:
        eid = _pick(e, "examId", "id")
        detail = result = None
        try:
            detail = api.exam_detail(eid)
        except Exception as exc:  # noqa: BLE001
            detail = {"error": str(exc)[:200]}
        try:
            result = api.get_result(eid)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc)[:200]}
        print(_describe(e, detail, result))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
