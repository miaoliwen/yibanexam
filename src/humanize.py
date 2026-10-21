"""拟人化节奏。

这里做的不是"破解风控"，而是让自动化行为在统计特征上接近真人：
思考时间服从对数正态分布（真人答题反应时间就是这种长尾分布）、
偶发长停顿、作答顺序不严格线性。

真正的检测规避靠的是 session.py 里的真实浏览器 —— 真实的 UA、
真实的 TLS 指纹、真实的 WebGL/Canvas、真实的风控 SDK 运行环境。
伪造这些参数只会让画像更异常。

v2 的变化：不再"全部答完再补时"（时间线是「瞬时答完 → 静默 N 分钟 → 交卷」，
本身就是破绽），改成 Pacer 逐题推进：每题的等待在答题过程中就消耗掉，
总时长、长尾停顿、交卷时刻都由同一套分布决定。
"""
from __future__ import annotations

import math
import random
import time
from typing import Sequence, TypeVar

from .config import HumanizeCfg

T = TypeVar("T")


def think_time_ms(cfg: HumanizeCfg, *, difficulty: float = 0.5) -> int:
    """模拟单题思考耗时（毫秒）。

    对数正态：多数集中在低值，少数很长，符合真人分布。
    difficulty ∈ [0,1]，题目越长/选项越多 → 越久。
    """
    if not cfg.enabled:
        return 0

    lo, hi = cfg.min_delay_ms, cfg.max_delay_ms
    if hi <= lo:
        return lo

    # 以 difficulty 把分布中心从 lo 推向 hi
    center = lo + (hi - lo) * (0.25 + 0.5 * difficulty)
    sigma = 0.35 + 0.25 * difficulty
    # 对数正态期望 = exp(mu + sigma^2/2)，这里用 mu=0 抽样后除以该系数校正
    val = random.lognormvariate(0, sigma) * center / math.exp(sigma * sigma / 2)

    if random.random() < cfg.long_think_rate:
        val += random.randint(cfg.long_think_ms // 2, cfg.long_think_ms)

    return int(max(lo, min(hi, val)))


def sleep_ms(ms: int) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


def pause(cfg: HumanizeCfg, *, difficulty: float = 0.5) -> int:
    ms = think_time_ms(cfg, difficulty=difficulty)
    sleep_ms(ms)
    return ms


def question_difficulty(stem: str, n_options: int, qtype: str) -> float:
    """粗略的难度估计，用于把思考时间拉开梯度。"""
    d = 0.3
    d += min(len(stem) / 400.0, 0.35)
    d += min(max(n_options - 2, 0) * 0.06, 0.2)
    if qtype in ("multi", "essay"):
        d += 0.15
    return max(0.0, min(1.0, d))


def shuffled(seq: Sequence[T], enabled: bool = True) -> list[T]:
    items = list(seq)
    if enabled:
        random.shuffle(items)
    return items


def jitter(cfg: HumanizeCfg) -> int:
    """交卷前的收尾停顿。"""
    if not cfg.enabled:
        return 0
    ms = cfg.idle_before_submit_ms + random.randint(0, 2500)
    sleep_ms(ms)
    return ms


class Pacer:
    """逐题节奏器：把整场耗时摊到每道题上，并受截止时间硬约束。

    设计：
    - 初始化时按 per_question_seconds 抽样一份"每题耗时计划"（对数正态，
      偶发长停顿），总和 ≈ 题量 × 均值；
    - 有时间预算时按比例缩放计划，保证在截止前留出交卷安全垫；
    - 每次 wait_next() 把这道题的等待"睡掉"，LLM 自己花的时间计入进度；
    - 剩余时间不足时自动进入 fast 模式（不再等待），把机会留给交卷。

    clock/sleep/rng 可注入，方便单元测试不真的睡觉。
    """

    def __init__(
        self,
        cfg: HumanizeCfg,
        *,
        started_at: float,
        total_questions: int,
        deadline_at: float | None = None,
        reserve_seconds: float = 120.0,
        max_total_seconds: float = 0.0,
        difficulties: Sequence[float] | None = None,
        clock=time.time,
        sleep=time.sleep,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.started_at = started_at
        self.total_questions = max(0, int(total_questions))
        self.deadline_at = deadline_at
        self.reserve_seconds = max(0.0, float(reserve_seconds))
        self.max_total_seconds = max(0.0, float(max_total_seconds))
        self.difficulties = list(difficulties or [])
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._idx = 0
        self._next_at = started_at
        self.fast = False
        self.plan: list[float] = []
        self._build_plan()

    # ------------------------------------------------------------------
    def remaining_seconds(self) -> float:
        if not self.deadline_at:
            return float("inf")
        return self.deadline_at - self.clock()

    def budget_seconds(self) -> float:
        """可用答题预算 = 截止剩余 - 安全垫（无截止时取 max_total 或 inf）。"""
        hard = self.remaining_seconds()
        if hard != float("inf"):
            hard -= self.reserve_seconds
        if self.max_total_seconds > 0:
            hard = min(hard, self.max_total_seconds)
        return hard

    def exhausted(self) -> bool:
        # 留 0.5s 容差：计划缩放到预算后，浮点误差不应让"刚好用完"误判为还有时间
        return self.budget_seconds() <= 0.5

    # ------------------------------------------------------------------
    def _build_plan(self) -> None:
        n = self.total_questions
        per = float(self.cfg.per_question_seconds)
        if not self.cfg.enabled or n <= 0 or per <= 0:
            self.fast = True
            return

        sigma = 0.45
        plan: list[float] = []
        for i in range(n):
            # 难度参与分布中心：难题想得久一点，简单题快一点。
            # 平均难度 0.5 时系数为 1，整场总量仍 ≈ n × per_question_seconds。
            d = self.difficulties[i] if i < len(self.difficulties) else 0.5
            d = max(0.0, min(1.0, float(d)))
            factor = 0.7 + 0.6 * d
            gap = per * factor * self.rng.lognormvariate(-0.5 * sigma * sigma, sigma)
            if self.rng.random() < self.cfg.long_think_rate:
                gap += (self.cfg.long_think_ms / 1000.0) * self.rng.uniform(0.5, 1.0)
            plan.append(min(max(gap, per * 0.25), per * 3.0))

        budget = self.budget_seconds()
        if budget != float("inf") and budget > 0:
            # 不要等比缩放到「正好用完预算」——那会让交卷时刻恒等于
            # 截止 − reserve（离线模拟窗口只有几秒，仍可被规则命中）。
            # 目标只占预算的 80%~97%，剩下的裕量本身就是随机交卷抖动。
            target = budget * self.rng.uniform(0.80, 0.97)
            total = sum(plan)
            if total > target:
                scale = max(target, 0.0) / total
                plan = [g * scale for g in plan]
                # 计划被压到不足原值 35% 时，说明时间已经很紧，别再演了
                self.fast = scale < 0.35
        self.plan = plan

    # ------------------------------------------------------------------
    def wait_next(self) -> float:
        """睡掉第 idx 题的等待。返回实际睡眠秒数（测试用）。"""
        if self._idx >= len(self.plan) or self.fast:
            return 0.0
        gap = self.plan[self._idx]
        self._idx += 1
        self._next_at += gap
        hard = self.budget_seconds()
        if hard <= 0:
            self.fast = True
            return 0.0
        wait = self._next_at - self.clock()
        if wait <= 0:
            return 0.0
        wait = min(wait, hard)
        if self.fast:
            wait = min(wait, 0.5)
        if wait > 0:
            self.sleep(wait)
        return wait


def pace_to(cfg: HumanizeCfg, started_at: float, per_question_seconds: float, n_questions: int) -> float:
    """【已弃用】交卷前一次性补足总时长。

    保留函数是为了兼容旧调用方；runner 已改为逐题 Pacer。
    一次性补时的形状是「瞬时答完 → 静默半小时 → 交卷」，不建议再用。
    """
    if not cfg.enabled or n_questions <= 0:
        return 0.0

    total = 0.0
    for _ in range(n_questions):
        total += per_question_seconds * random.lognormvariate(-0.15, 0.45)

    elapsed = time.time() - started_at
    if elapsed >= total:
        return 0.0

    wait = total - elapsed
    chunk = 5.0
    left = wait
    while left > 0:
        time.sleep(min(chunk, left))
        left -= chunk
    return wait
