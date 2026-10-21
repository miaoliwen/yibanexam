"""把「答案 TXT」转成结构化题库 JSON。

支持的 TXT 形态（本项目 入学教育答案.txt 就是这种）：

    1.
    题干：南通师范高等专科学校肇端于（ B）的民立通州师范学校……
    A. 1901年
    B. 1902年
    C. 1903年
    D. 1904年

    ...
    7.
    题干：如果在我校图书馆借阅的图书需要续借，需在（ ）办理续借手续。 C
    A. 书借阅到期后
    ...

也就是答案字母可能有两种位置：
1. 题干预设空里：`（ B）` / `（B ）` / `(B)`，支持多选 `（AB）`；
2. 题干末尾独立一个字母：`……手续。 C`（前面必须有空白，避免误吃正文里的字母）。

用法（CLI）：
    python main.py --build-bank "答案.txt"
    python main.py --build-bank xxx.txt --bank data/question_bank.json --bank-name 入学教育

产物结构：
    {
      "name": "...", "version": 1, "source": "...", "created_at": "...",
      "count": 50,
      "items": [
        {"id": "q1", "no": 1, "qtype": "single", "stem": "...",
         "norm": "归一化题干（去标点空白，用于精确命中）",
         "options": {"A": "...", "B": "..."},
         "answer": ["B"], "answer_texts": ["1902年"]}
      ]
    }
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

from .qbank import normalize, option_key_set

BLOCK_RE = re.compile(r"^\s*(\d{1,4})\s*[.．、]\s*(.*)$")
OPT_RE = re.compile(r"^\s*([A-Ha-h])\s*[.．、\)）]\s*(.+?)\s*$")
STEM_PREFIX_RE = re.compile(r"^\s*(?:题干|题目|问题)\s*[:：]\s*")
# 空里的答案字母：允许全角/半角括号、内部空白；1~4 个字母视为多选
INLINE_ANS_RE = re.compile(r"[（(]\s*([A-Ha-h]{1,4})\s*[）)]")
# 题干末尾独立字母（前面必须是空白，且后面只剩空白）
TRAIL_ANS_RE = re.compile(r"[ \t\u3000]+([A-Ha-h])[ \t\u3000]*$")

JUDGE_TEXTS = {"对", "错", "正确", "错误", "是", "否", "√", "×", "t", "f", "true", "false"}


def _clean_line(line: str) -> str:
    return line.replace("\ufeff", "").rstrip("\r\n")


def _detect_qtype(options: dict[str, str]) -> str:
    if len(options) == 2:
        texts = {str(v).strip().lower() for v in options.values()}
        if texts and texts <= JUDGE_TEXTS:
            return "judge"
    if 2 <= len(options) <= 3 and all(len(str(v).strip()) <= 1 for v in options.values()):
        if {str(v).strip().upper() for v in options.values()} <= {"T", "F", "√", "×"}:
            return "judge"
    return "single" if options else "fill"


def _extract_answer(stem: str) -> tuple[str, list[str], str]:
    """从题干里抽出答案字母。返回 (清理后的题干, 字母列表, 命中方式)。"""
    inline = INLINE_ANS_RE.search(stem)
    if inline:
        letters = [c.upper() for c in inline.group(1)]
        cleaned = stem[: inline.start()] + "（）" + stem[inline.end() :]
        return cleaned, letters, "inline"

    trail = TRAIL_ANS_RE.search(stem)
    if trail:
        letters = [trail.group(1).upper()]
        cleaned = stem[: trail.start()].rstrip()
        return cleaned, letters, "trail"

    return stem, [], ""


def parse_answer_txt(text: str) -> list[dict[str, Any]]:
    """解析答案 TXT，返回题目条目列表（保持原顺序）。"""
    lines = [_clean_line(x) for x in str(text or "").splitlines()]
    blocks: list[tuple[int, list[str]]] = []
    cur_no: int | None = None
    cur_lines: list[str] = []
    last_no = 0

    for raw in lines:
        m = BLOCK_RE.match(raw)
        is_start = False
        if m:
            num = int(m.group(1))
            # 只认「连续编号」的块首，避免题干里的 "2.5倍" 之类被当成新题
            is_start = num == last_no + 1 or (last_no == 0 and num == 1)
        if is_start and m:
            if cur_no is not None:
                blocks.append((cur_no, cur_lines))
            cur_no = int(m.group(1))
            last_no = cur_no
            rest = m.group(2).strip()
            cur_lines = [rest] if rest else []
        elif cur_no is not None:
            cur_lines.append(raw)
    if cur_no is not None:
        blocks.append((cur_no, cur_lines))

    items: list[dict[str, Any]] = []
    for no, blines in blocks:
        stem_parts: list[str] = []
        options: dict[str, str] = {}
        in_options = False
        for line in blines:
            if not line.strip():
                continue
            om = OPT_RE.match(line)
            if om:
                in_options = True
                letter = om.group(1).upper()
                text_ = om.group(2).strip()
                # 选项文本可能接着续行，这里只收第一行（本文件没有续行）
                options.setdefault(letter, text_)
                continue
            if not in_options:
                stem_parts.append(line)
            else:
                # 选项之后的正文极少见：并到上一个选项
                if options:
                    k = list(options)[-1]
                    options[k] = f"{options[k]} {line.strip()}".strip()

        if not stem_parts:
            continue
        stem = " ".join(p.strip() for p in stem_parts)
        stem = STEM_PREFIX_RE.sub("", stem).strip()
        stem, letters, how = _extract_answer(stem)
        stem = re.sub(r"[ \t\u3000]+", " ", stem).strip()

        # 题干里没写出答案（或写错字母）的题，保留但标记
        valid = option_key_set(options)
        letters = [x for x in letters if (not valid or x in valid)]
        qtype = _detect_qtype(options)
        answer_texts = [options.get(k, "") for k in letters if options.get(k)]
        items.append(
            {
                "id": f"q{no}",
                "no": no,
                "qtype": qtype,
                "stem": stem,
                "norm": normalize(stem),
                "options": options,
                "answer": letters,
                "answer_texts": answer_texts,
                "answer_from": how,
            }
        )
    return items


def build_bank(
    txt_path: str | Path,
    out_path: str | Path | None = None,
    *,
    name: str = "",
    verbose: bool = True,
) -> dict[str, Any]:
    """读 TXT → 写 JSON，返回题库字典。"""
    src = Path(txt_path)
    if not src.exists():
        raise FileNotFoundError(f"答案文件不存在：{src}")
    text = src.read_text(encoding="utf-8-sig", errors="replace")
    items = parse_answer_txt(text)
    if not items:
        raise ValueError(
            "没有解析出任何题目。请确认 TXT 是「1. / 题干： / A. B. C. D.」这种结构。"
        )

    missing = [it["no"] for it in items if not it["answer"]]
    bank = {
        "name": name or src.stem,
        "version": 1,
        "source": str(src),
        "created_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "items": items,
    }

    if out_path:
        dst = Path(out_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if verbose:
        stats: dict[str, int] = {}
        for it in items:
            stats[it["qtype"]] = stats.get(it["qtype"], 0) + 1
        print(f"[题库] 解析到 {len(items)} 题：{stats}")
        if out_path:
            print(f"[题库] 已写入 {out_path}")
        if missing:
            msg = f"[题库] 有 {len(missing)} 题没解析出答案：第 {missing} 题"
            print(msg, file=sys.stderr)
    return bank


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="答案 TXT → 题库 JSON")
    ap.add_argument("txt")
    ap.add_argument("-o", "--out", default="data/question_bank.json")
    ap.add_argument("-n", "--name", default="")
    a = ap.parse_args()
    build_bank(a.txt, a.out, name=a.name)
