from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from workflaskcode.config.models import Limits
from workflaskcode.errors import WorkFlaskError
from workflaskcode.providers.types import ModelInfo, Usage
from workflaskcode.state.db import Database


class BudgetExceeded(WorkFlaskError):
    pass


@dataclass
class Totals:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    errors: int = 0
    rate_limit_events: int = 0
    duration_ms: int = 0


class UsageTracker:
    def __init__(self, db: Database, session_id: str | None = None) -> None:
        self.db = db
        self.session_id = session_id
        self.price_book: dict[str, tuple[float | None, float | None]] = {}
        self.free_models: set[str] = set()

    def load_prices(self, provider: str, models: list[ModelInfo]) -> None:
        for m in models:
            key = f"{provider}:{m.id}"
            self.price_book[key] = (m.prompt_price, m.completion_price)
            if m.is_free:
                self.free_models.add(key)

    def estimate_cost(self, provider: str, model: str, usage: Usage) -> tuple[float | None, bool]:
        if usage.cost is not None:
            return usage.cost, False
        key = f"{provider}:{model}"
        if key in self.free_models:
            return 0.0, False
        prices = self.price_book.get(key)
        if prices and prices[0] is not None and prices[1] is not None:
            return usage.input_tokens * prices[0] + usage.output_tokens * prices[1], True
        return None, False

    def record(
        self,
        provider: str,
        model: str,
        usage: Usage,
        *,
        duration_ms: int = 0,
        task_id: str | None = None,
        error_kind: str | None = None,
        channel: str = "main",
    ) -> None:
        cost, estimated = self.estimate_cost(provider, model, usage)
        self.db.execute(
            "INSERT INTO usage(ts,session_id,task_id,provider,model,input_tokens,output_tokens,cached_tokens,"
            "cost,cost_estimated,duration_ms,error_kind,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                self.session_id,
                task_id,
                provider,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                cost,
                int(estimated),
                duration_ms,
                error_kind,
                channel,
            ),
        )

    def totals(
        self, *, session: bool = True, task_id: str | None = None, since: float | None = None
    ) -> Totals:
        sql = "SELECT * FROM usage WHERE 1=1"
        params: list[Any] = []
        if session and self.session_id:
            sql += " AND session_id=?"
            params.append(self.session_id)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if since:
            sql += " AND ts>=?"
            params.append(since)
        out = Totals()
        for r in self.db.query(sql, params):
            out.requests += 1
            out.input_tokens += r["input_tokens"]
            out.output_tokens += r["output_tokens"]
            out.cached_tokens += r["cached_tokens"]
            out.duration_ms += r["duration_ms"]
            if r["error_kind"]:
                out.errors += 1
                if r["error_kind"] in ("rate_limit_model", "quota_account"):
                    out.rate_limit_events += 1
            if r["cost"] is None and not r["error_kind"]:
                out.cost_known = False
            else:
                out.cost += r["cost"] or 0.0
        return out

    def by_model(self, *, session: bool = True) -> list[dict[str, Any]]:
        sql = (
            "SELECT provider, model, COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
            "SUM(cached_tokens) AS c, SUM(cost) AS cost, SUM(cost IS NULL AND error_kind IS NULL) AS unknown_cost, "
            "SUM(cost_estimated) AS est, SUM(error_kind IS NOT NULL) AS errs FROM usage"
        )
        params: list[Any] = []
        if session and self.session_id:
            sql += " WHERE session_id=?"
            params.append(self.session_id)
        sql += " GROUP BY provider, model ORDER BY n DESC"
        return [dict(r) for r in self.db.query(sql, params)]

    def recent_rate_limits(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT ts, provider, model, error_kind FROM usage WHERE error_kind IN ('rate_limit_model','quota_account','overloaded') "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def check_budget(self, limits: Limits, *, task_id: str | None, provider: str, model: str) -> None:
        if task_id:
            t = self.totals(session=False, task_id=task_id)
            if t.requests >= limits.max_requests_per_task:
                raise BudgetExceeded(
                    f"task reached the request limit ({limits.max_requests_per_task})",
                    hint="raise limits.max_requests_per_task or split the task",
                )
            if limits.max_cost_per_task is not None and t.cost >= limits.max_cost_per_task:
                raise BudgetExceeded(
                    f"task reached the cost limit (${limits.max_cost_per_task:.4f}, spent ${t.cost:.4f})",
                    hint="raise limits.max_cost_per_task",
                )
        key = f"{provider}:{model}"
        budget = limits.model_budgets.get(key)
        if budget is not None:
            spent = sum(
                (r["cost"] or 0.0)
                for r in self.db.query(
                    "SELECT cost FROM usage WHERE provider=? AND model=?", (provider, model)
                )
            )
            if spent >= budget:
                raise BudgetExceeded(f"model budget for {key} is used up (${spent:.4f} of ${budget:.4f})")

    @staticmethod
    def format_totals(t: Totals) -> str:
        cost = f"${t.cost:.4f}" + ("" if t.cost_known else " (+ unknown-cost requests)")
        return (
            f"requests {t.requests} | in {t.input_tokens} | out {t.output_tokens} | cached {t.cached_tokens} | "
            f"cost {cost} | errors {t.errors} | rate-limit events {t.rate_limit_events}"
        )
