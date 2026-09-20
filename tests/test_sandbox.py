"""方案沙盒功能测试（仅标准库，风格与 test_smoke 一致）。

运行：python -m tests.test_sandbox

覆盖：
* 预览不改动 settings / assignments / 锁定 / last_solution；
* 预览差异：新增/移除评审配对、总成本、未满足人数、主审缺口变化；
* 版本指纹覆盖稿件/评审人/冲突/锁定与参数，且不受求解输出（非锁定分配、
  last_solution）影响；
* 采用：指纹一致时单事务写入临时参数与预览分配；指纹过期返回 409 且不写入，
  重新预览后恢复可采用；
* API 层（安装了 fastapi 时）：预览/采用端点与 409。
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-sandbox-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, get_setting, init_db, set_setting  # noqa: E402
from app import sandbox  # noqa: E402
from app.sandbox import SandboxError  # noqa: E402
from app.solver import solve  # noqa: E402

init_db(seed_demo=False)


# ------------------------------------------------------------- 测试夹具
def _reset(conn) -> None:
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM conflict_declarations")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    set_setting(conn, "mismatch_weight", 10)
    set_setting(conn, "load_weight", 2)
    set_setting(conn, "institution_cap", 0)
    set_setting(conn, "last_solution", None)
    conn.commit()


def _add_m(conn, title, topics, req=1, leads=0) -> int:
    cur = conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_leads) "
        "VALUES (?, ?, ?, ?)",
        (title, json.dumps(topics, ensure_ascii=False), req, leads))
    return cur.lastrowid


def _add_r(conn, name, topics, cap=3, inst="", can_lead=0) -> int:
    cur = conn.execute(
        "INSERT INTO reviewers(name, topics, capacity, institution, can_lead) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, json.dumps(topics, ensure_ascii=False), cap, inst, can_lead))
    return cur.lastrowid


def _pairs(conn) -> set[tuple[int, int]]:
    return {(r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments")}


def _weights_scenario(conn):
    """两篇稿件、两名评审：β=10/α=2 时最优 {(m1,r2),(m2,r1)}（费用 4）；
    β=0/α=0 时全部边零费用，确定性求解得 {(m1,r1),(m2,r1)}（费用 0）。"""
    _reset(conn)
    m1 = _add_m(conn, "稿1", ["a"])
    m2 = _add_m(conn, "稿2", ["b"])
    r1 = _add_r(conn, "r1", ["a", "b"], cap=2)
    r2 = _add_r(conn, "r2", ["a"], cap=2)
    conn.commit()
    return m1, m2, r1, r2


# ------------------------------------------------------------- 预览只读
def test_preview_does_not_touch_state() -> None:
    conn = _connect()
    try:
        m1, m2, r1, r2 = _weights_scenario(conn)
        solve(conn)
        before_settings = {k: get_setting(conn, k) for k in
                           ("mismatch_weight", "load_weight", "institution_cap")}
        before_assign = [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")]
        before_solution = get_setting(conn, "last_solution")

        pv = sandbox.preview(conn, 0, 0, 0)

        # settings / assignments / last_solution 均未改动
        assert {k: get_setting(conn, k) for k in before_settings} == before_settings
        after_assign = [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")]
        assert after_assign == before_assign
        assert get_setting(conn, "last_solution") == before_solution

        # 预览内容完整：参数、指纹、差异与完整解
        assert pv["params"] == {"mismatch_weight": 0, "load_weight": 0, "institution_cap": 0}
        assert len(pv["fingerprint"]) == 64
        assert pv["solution"]["total_cost"] == 0
        assert "added_pairs" in pv["diff"] and "removed_pairs" in pv["diff"]
    finally:
        conn.close()


# ------------------------------------------------------------- 预览差异
def test_preview_diff_pairs_and_cost() -> None:
    conn = _connect()
    try:
        m1, m2, r1, r2 = _weights_scenario(conn)
        sol = solve(conn)  # β=10, α=2
        assert sol["total_cost"] == 4
        assert _pairs(conn) == {(m1, r2), (m2, r1)}

        pv = sandbox.preview(conn, 0, 0, 0)
        d = pv["diff"]
        added = {(p["manuscript_id"], p["reviewer_id"]) for p in d["added_pairs"]}
        removed = {(p["manuscript_id"], p["reviewer_id"]) for p in d["removed_pairs"]}
        assert added == {(m1, r1)}
        assert removed == {(m1, r2)}
        assert d["added_pairs"][0]["manuscript_title"] == "稿1"
        assert d["added_pairs"][0]["reviewer_name"] == "r1"
        assert d["total_cost"] == {"current": 4, "preview": 0, "delta": -4}
        assert d["unmet_people"] == {"current": 0, "preview": 0, "delta": 0}
        assert d["lead_deficit"] == {"current": 0, "preview": 0, "delta": 0}
    finally:
        conn.close()


def test_preview_diff_unmet_and_lead_deficit() -> None:
    """机构上限变化引起未满足人数与主审缺口变化；锁定行保留在预览配对中。"""
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "稿1", ["t"], req=2, leads=1)
        r1 = _add_r(conn, "q1", ["t"], cap=2, inst="A", can_lead=1)
        r2 = _add_r(conn, "x1", ["t"], cap=2, inst="A", can_lead=0)
        # 锁定非合资格的 x1：γ=1 时 A 机构名额被锁占满 → q1 进不来 → 缺 1 人且缺主审
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m1, r2))
        set_setting(conn, "institution_cap", 1)
        conn.commit()
        sol = solve(conn)
        assert sol["unmet"][0]["deficit"] == 1
        assert sol["unmet"][0]["lead_deficit"] == 1

        pv = sandbox.preview(conn, 10, 2, 2)  # 放宽机构上限 → q1 作为主审进入
        d = pv["diff"]
        assert d["unmet_people"] == {"current": 1, "preview": 0, "delta": -1}
        assert d["lead_deficit"] == {"current": 1, "preview": 0, "delta": -1}
        assert d["unmet_manuscripts"] == {"current": 1, "preview": 0, "delta": -1}
        added = {(p["manuscript_id"], p["reviewer_id"]) for p in d["added_pairs"]}
        assert added == {(m1, r1)}
        assert d["added_pairs"][0]["is_lead"] is True
        assert d["removed_pairs"] == []          # 锁定行原样保留，无移除
        assert pv["solution"]["fully_satisfied"]
    finally:
        conn.close()


# ------------------------------------------------------------- 版本指纹
def test_fingerprint_covers_inputs_not_outputs() -> None:
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "稿1", ["a"])
        r1 = _add_r(conn, "r1", ["a"], cap=2)
        r2 = _add_r(conn, "r2", ["a"], cap=2)
        conn.commit()
        fp = sandbox.fingerprint(conn, 10, 2, 0)

        # 参数变化 → 指纹变化
        assert sandbox.fingerprint(conn, 11, 2, 0) != fp
        assert sandbox.fingerprint(conn, 10, 3, 0) != fp
        assert sandbox.fingerprint(conn, 10, 2, 1) != fp

        # 冲突变化 → 指纹变化
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m1, r2))
        conn.commit()
        assert sandbox.fingerprint(conn, 10, 2, 0) != fp
        conn.execute("DELETE FROM conflicts")

        # 锁定变化 → 指纹变化
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m1, r1))
        conn.commit()
        assert sandbox.fingerprint(conn, 10, 2, 0) != fp
        conn.execute("DELETE FROM assignments")

        # 评审人/稿件变化 → 指纹变化
        conn.execute("UPDATE reviewers SET capacity = 5 WHERE id = ?", (r2,))
        conn.commit()
        assert sandbox.fingerprint(conn, 10, 2, 0) != fp
        conn.execute("UPDATE reviewers SET capacity = 2 WHERE id = ?", (r2,))
        conn.execute("UPDATE manuscripts SET title = '稿1改' WHERE id = ?", (m1,))
        conn.commit()
        assert sandbox.fingerprint(conn, 10, 2, 0) != fp
        conn.execute("UPDATE manuscripts SET title = '稿1' WHERE id = ?", (m1,))
        conn.commit()
        assert sandbox.fingerprint(conn, 10, 2, 0) == fp

        # 求解输出（非锁定分配、last_solution）变化 → 指纹不变
        solve(conn)
        assert _pairs(conn)
        assert sandbox.fingerprint(conn, 10, 2, 0) == fp
    finally:
        conn.close()


# ------------------------------------------------------------- 采用
def test_apply_writes_params_and_preview_assignments() -> None:
    conn = _connect()
    try:
        m1, m2, r1, r2 = _weights_scenario(conn)
        solve(conn)
        pv = sandbox.preview(conn, 0, 0, 0)

        sol = sandbox.apply(conn, 0, 0, 0, pv["fingerprint"])

        # 临时参数已写入设置
        assert get_setting(conn, "mismatch_weight") == 0
        assert get_setting(conn, "load_weight") == 0
        assert get_setting(conn, "institution_cap") == 0
        # 预览分配已落库，last_solution 同步更新
        assert _pairs(conn) == {(m1, r1), (m2, r1)}
        assert sol["total_cost"] == pv["solution"]["total_cost"] == 0
        assert get_setting(conn, "last_solution")["total_cost"] == 0
        # 一键求解流程仍可用（按新参数重算，结果一致）
        assert solve(conn)["total_cost"] == 0
        assert _pairs(conn) == {(m1, r1), (m2, r1)}
    finally:
        conn.close()


def test_apply_stale_fingerprint_returns_409_and_writes_nothing() -> None:
    conn = _connect()
    try:
        m1, m2, r1, r2 = _weights_scenario(conn)
        solve(conn)
        pv = sandbox.preview(conn, 0, 0, 0)

        # 并发修改：新增冲突（指纹覆盖的任一项即可使预览过期）
        conn.execute("INSERT INTO conflicts(m_id, r_id) VALUES (?, ?)", (m2, r1))
        conn.commit()
        before_assign = [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")]

        try:
            sandbox.apply(conn, 0, 0, 0, pv["fingerprint"])
            raise AssertionError("应当返回 409")
        except SandboxError as e:
            assert e.status_code == 409
            assert "重新" in e.message

        # 未写入：参数、分配、last_solution 全部保持
        assert get_setting(conn, "mismatch_weight") == 10
        assert get_setting(conn, "load_weight") == 2
        after_assign = [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id, locked FROM assignments ORDER BY m_id, r_id")]
        assert after_assign == before_assign
        assert get_setting(conn, "last_solution")["total_cost"] == 4

        # 指纹绑定参数：用其他参数套用同一指纹同样 409
        pv2 = sandbox.preview(conn, 0, 0, 0)
        try:
            sandbox.apply(conn, 1, 0, 0, pv2["fingerprint"])
            raise AssertionError("应当返回 409")
        except SandboxError as e:
            assert e.status_code == 409

        # 重新预览后可正常采用
        sol = sandbox.apply(conn, 0, 0, 0, pv2["fingerprint"])
        assert get_setting(conn, "mismatch_weight") == 0
        assert sol["total_cost"] == 0
    finally:
        conn.close()


def test_apply_keeps_locks_and_replaces_only_unlocked() -> None:
    conn = _connect()
    try:
        _reset(conn)
        m1 = _add_m(conn, "稿1", ["t"], req=2)
        r1 = _add_r(conn, "r1", ["t"], cap=2)
        r2 = _add_r(conn, "r2", ["t"], cap=2)
        conn.execute("INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1)", (m1, r1))
        conn.commit()
        solve(conn)
        pv = sandbox.preview(conn, 10, 2, 0)
        sandbox.apply(conn, 10, 2, 0, pv["fingerprint"])
        locked = [tuple(r) for r in conn.execute(
            "SELECT m_id, r_id FROM assignments WHERE locked = 1")]
        assert locked == [(m1, r1)]               # 锁定行原样保留
        assert _pairs(conn) == {(m1, r1), (m1, r2)}
    finally:
        conn.close()


# ------------------------------------------------------------- API 层
def test_sandbox_api() -> None:
    try:
        from app.main import sandbox_apply, sandbox_preview
        from app.schemas import SandboxApplyIn, SandboxPreviewIn
    except ModuleNotFoundError:
        print("  （跳过接口测试：未安装 fastapi/pydantic）")
        return

    conn = _connect()
    try:
        m1, m2, r1, r2 = _weights_scenario(conn)
        solve(conn)

        pv = sandbox_preview(SandboxPreviewIn(
            mismatch_weight=0, load_weight=0, institution_cap=0), conn)
        assert pv["diff"]["total_cost"]["preview"] == 0
        assert pv["diff"]["total_cost"]["current"] == 4

        # 过期指纹 → 409（由异常处理器转为 JSON 响应）
        conn.execute("INSERT INTO reviewers(name, topics, capacity) VALUES ('r3', '[]', 1)")
        conn.commit()
        try:
            sandbox_apply(SandboxApplyIn(
                mismatch_weight=0, load_weight=0, institution_cap=0,
                fingerprint=pv["fingerprint"]), conn)
            raise AssertionError("应当 409")
        except SandboxError as e:
            assert e.status_code == 409

        # 重新预览后采用成功，返回完整状态
        pv2 = sandbox_preview(SandboxPreviewIn(
            mismatch_weight=0, load_weight=0, institution_cap=0), conn)
        st = sandbox_apply(SandboxApplyIn(
            mismatch_weight=0, load_weight=0, institution_cap=0,
            fingerprint=pv2["fingerprint"]), conn)
        assert st["settings"]["mismatch_weight"] == 0
        assert st["solution"]["total_cost"] == 0
        assert "manuscripts" in st and "assignments" in st and "reviewers" in st
    finally:
        conn.close()


if __name__ == "__main__":
    test_preview_does_not_touch_state()
    test_preview_diff_pairs_and_cost()
    test_preview_diff_unmet_and_lead_deficit()
    test_fingerprint_covers_inputs_not_outputs()
    test_apply_writes_params_and_preview_assignments()
    test_apply_stale_fingerprint_returns_409_and_writes_nothing()
    test_apply_keeps_locks_and_replaces_only_unlocked()
    test_sandbox_api()
    print("所有方案沙盒测试通过 ✅")
