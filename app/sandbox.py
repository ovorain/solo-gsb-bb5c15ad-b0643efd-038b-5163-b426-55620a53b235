"""方案沙盒：临时求解参数的预览与采用。

* 预览（preview）：用临时参数（主题不匹配权重 β、负载权重 α、机构上限 γ）
  在内存中重跑两阶段 MCMF，**不写入** settings、assignments、锁定项或
  last_solution；返回与当前方案的差异（新增/移除评审配对、总成本、
  未满足人数、主审缺口变化）以及绑定当前数据与参数的版本指纹。
* 采用（apply）：仅当指纹仍一致时，在**单个事务**（BEGIN IMMEDIATE）内
  写入临时参数与预览分配；否则返回 409 并提示重新预览，
  避免过期结果覆盖并发修改。

版本指纹
========
指纹覆盖求解器的全部输入：稿件、评审人、冲突、锁定分配与三个求解参数。
非锁定分配与 last_solution 是求解**输出**，不参与指纹——因此预览后
再次一键求解（同参数）不会使预览失效，但任何数据/参数改动都会。
求解器对相同输入是确定性的，指纹一致即保证重算结果与预览完全一致。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting
from .solver import compute_solution, write_solution


class SandboxError(Exception):
    """业务异常：status_code 为应返回的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ------------------------------------------------------------- 版本指纹
def fingerprint(conn: sqlite3.Connection, beta: int, alpha: int, org_cap: int) -> str:
    """求解全部输入（稿件/评审人/冲突/锁定 + 三个参数）的 SHA-256 版本指纹。"""
    payload = {
        "params": {
            "mismatch_weight": beta,
            "load_weight": alpha,
            "institution_cap": org_cap,
        },
        "manuscripts": [
            [r["id"], r["title"], json.loads(r["topics"]),
             r["required_reviews"], r["required_leads"]]
            for r in conn.execute("SELECT * FROM manuscripts ORDER BY id")
        ],
        "reviewers": [
            [r["id"], r["name"], json.loads(r["topics"]), r["capacity"],
             r["institution"] or "", 1 if r["can_lead"] else 0]
            for r in conn.execute("SELECT * FROM reviewers ORDER BY id")
        ],
        "conflicts": [
            [r["m_id"], r["r_id"]]
            for r in conn.execute("SELECT m_id, r_id FROM conflicts ORDER BY m_id, r_id")
        ],
        "locks": [
            [r["m_id"], r["r_id"]]
            for r in conn.execute(
                "SELECT m_id, r_id FROM assignments WHERE locked = 1 ORDER BY m_id, r_id")
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 预览
def _metrics(sol: dict) -> dict:
    return {
        "total_cost": sol["total_cost"],
        "unmet_people": sum(u["deficit"] for u in sol["unmet"]),
        "unmet_manuscripts": len(sol["unmet"]),
        "lead_deficit": sum(u["lead_deficit"] for u in sol["unmet"]),
    }


def preview(conn: sqlite3.Connection, beta: int, alpha: int, org_cap: int) -> dict:
    """用临时参数生成预览：纯只读，不改动任何表。"""
    solution, lead_pairs, free_pairs = compute_solution(conn, beta, alpha, org_cap)
    fp = fingerprint(conn, beta, alpha, org_cap)

    # 采用后 assignments 表的内容 = 现有锁定行（原样保留）+ 新求解的非锁定配对
    locked_pairs = {
        (r["m_id"], r["r_id"])
        for r in conn.execute("SELECT m_id, r_id FROM assignments WHERE locked = 1")
    }
    current_pairs = {
        (r["m_id"], r["r_id"])
        for r in conn.execute("SELECT m_id, r_id FROM assignments")
    }
    preview_pairs = locked_pairs | set(lead_pairs) | set(free_pairs)
    added = sorted(preview_pairs - current_pairs)
    removed = sorted(current_pairs - preview_pairs)

    m_titles = {r["id"]: r["title"]
                for r in conn.execute("SELECT id, title FROM manuscripts")}
    r_names = {r["id"]: r["name"]
               for r in conn.execute("SELECT id, name FROM reviewers")}
    lead_set = {tuple(p) for p in solution["lead_pairs"]}

    def describe(pairs: list[tuple[int, int]]) -> list[dict]:
        return [
            {"manuscript_id": m_id, "reviewer_id": r_id,
             "manuscript_title": m_titles.get(m_id, f"#{m_id}"),
             "reviewer_name": r_names.get(r_id, f"#{r_id}"),
             "is_lead": (m_id, r_id) in lead_set}
            for m_id, r_id in pairs
        ]

    # “当前方案”指标取自上次求解结果（last_solution）；从未求解时为空（前端显示 —）
    last = get_setting(conn, "last_solution")
    cur = _metrics(last) if last else None
    nxt = _metrics(solution)

    def delta(key: str) -> dict:
        return {
            "current": cur[key] if cur else None,
            "preview": nxt[key],
            "delta": (nxt[key] - cur[key]) if cur else None,
        }

    return {
        "fingerprint": fp,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"mismatch_weight": beta, "load_weight": alpha,
                   "institution_cap": org_cap},
        "solution": solution,
        "diff": {
            "added_pairs": describe(added),
            "removed_pairs": describe(removed),
            "total_cost": delta("total_cost"),
            "unmet_people": delta("unmet_people"),
            "unmet_manuscripts": delta("unmet_manuscripts"),
            "lead_deficit": delta("lead_deficit"),
        },
    }


# ----------------------------------------------------------------- 采用
def apply(conn: sqlite3.Connection, beta: int, alpha: int, org_cap: int,
          expected_fingerprint: str) -> dict:
    """采用预览：指纹一致时在单个事务内写入临时参数与预览分配，否则 409。

    指纹一致 ⇒ 求解输入与预览时完全相同 ⇒ 重算结果与预览逐对一致，
    因此事务内重算并落库等价于“写入预览分配”，且不信赖客户端回传的配对。
    """
    # BEGIN IMMEDIATE：立即获取写锁，把“指纹校验 + 重算 + 写入”串行化，
    # 并发采用/并发修改要么等待后看到新指纹（409），不会互相覆盖。
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_fp = fingerprint(conn, beta, alpha, org_cap)
        if current_fp != expected_fingerprint:
            conn.execute("ROLLBACK")
            raise SandboxError(
                409,
                "预览生成后稿件、评审人、冲突、锁定或求解参数已发生变化，"
                "该预览结果已过期，本次未写入任何内容；请重新生成预览后再采用。")
        solution, lead_pairs, free_pairs = compute_solution(conn, beta, alpha, org_cap)
        set_setting(conn, "mismatch_weight", beta)
        set_setting(conn, "load_weight", alpha)
        set_setting(conn, "institution_cap", org_cap)
        write_solution(conn, solution, lead_pairs, free_pairs)
        conn.execute("COMMIT")
    except SandboxError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return solution
