"""What each verb needs prepared, and how it is run, for one rep.

Every rep works on branches forked from the point's own branch, so reps
never share state, and only the verb itself is timed. The writes a verb
carries are range updates of ``stock`` in warehouse 1 (the statement S1
issues on every backend), so the number of changed rows is exactly the
axis value and identical across systems.

Topology of the two-sided verbs: the source of a merge is forked from
its target, and a rebased branch from its upstream, because the
simulated merges find their base at the fork point of the side that
descends from the other.
"""

from dataclasses import dataclass, field

from dblib.db_api import DBToolSuite, OpResult
from microbench.verbs.grid import FOLLOW_UP_ROWS, STOCK_ROWS, key_ranges

ROWS_PER_HISTORY_COMMIT = 10


@dataclass
class Ctx:
    suite: DBToolSuite
    point: str                     # the point's branch, forked from the base
    rep: int = 0
    stock_rows: int = STOCK_ROWS
    log: callable = print


# -- untimed helpers ------------------------------------------------------

def update_rows(suite: DBToolSuite, ref: str, start: int, n: int, bump: int = 1,
                w_id: int = 1) -> None:
    """Change ``n`` stock rows of warehouse ``w_id`` from key ``start``."""
    if n <= 0:
        return

    def script(db):
        db.sql(f"UPDATE stock SET s_quantity = s_quantity + {int(bump)}, "
               f"s_remote_cnt = s_remote_cnt + {int(bump)} "
               "WHERE s_w_id = %s AND s_i_id BETWEEN %s AND %s", (w_id, start, start + n - 1))
    suite.exec(script, refs=[ref], timed=False, raise_on_error=True)


def commit(suite: DBToolSuite, ref: str, message: str) -> str:
    return suite.commit(ref, message, timed=False, raise_on_error=True).value


def fork(suite: DBToolSuite, name: str, from_ref: str) -> str:
    suite.branch(name, from_ref=from_ref, timed=False, raise_on_error=True)
    return name


def drop(suite: DBToolSuite, name: str) -> None:
    """Best effort; a branch the verb already deleted is fine."""
    try:
        suite.delete(name, timed=False)
    except Exception:
        pass


def commits_of(suite: DBToolSuite, ref: str, k: int, rows_each: int, start: int) -> list:
    """``k`` commits on ``ref``, each after an update of ``rows_each``
    rows, laid out one after another from key ``start``. Returns ids."""
    ids = []
    for i in range(k):
        update_rows(suite, ref, start + i * rows_each, rows_each)
        ids.append(commit(suite, ref, f"commit {i + 1} of {k}"))
    return ids


# -- verbs ----------------------------------------------------------------

class Verb:
    """One verb: what to prepare per point and per rep, the timed call,
    where the deferred follow-up writes go, and the cleanup."""

    name = ""
    deferred = False   # storage is also read after FOLLOW_UP_ROWS updates

    def prepare_point(self, ctx: Ctx, p: dict) -> dict:
        return {}

    def prepare_rep(self, ctx: Ctx, p: dict, ps: dict) -> dict:
        return {}

    def run(self, ctx: Ctx, p: dict, ps: dict, rs: dict, timed: bool, label: str):
        raise NotImplementedError

    def deferred_ref(self, rs: dict):
        return None

    def cleanup_rep(self, ctx: Ctx, rs: dict) -> None:
        pass

    def cleanup_point(self, ctx: Ctx, ps: dict) -> None:
        pass


class Branch(Verb):
    name = "branch"
    deferred = True

    def prepare_point(self, ctx, p):
        ps = {"source": ctx.point, "live": []}
        if p["age"] > 0:
            # age + 1 commits: the first is ``age`` behind the head.
            ids = commits_of(ctx.suite, ctx.point, p["age"] + 1, ROWS_PER_HISTORY_COMMIT, 1)
            ps["source"] = f"{ctx.point}@{ids[0]}"
        for i in range(p["live"] - 1):
            ps["live"].append(fork(ctx.suite, f"{ctx.point}l{i}", ctx.point))
        return ps

    def prepare_rep(self, ctx, p, ps):
        return {"name": f"{ctx.point}b{ctx.rep}"}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.branch(rs["name"], from_ref=ps["source"], timed=timed, label=label)

    def deferred_ref(self, rs):
        return rs["name"]

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["name"])

    def cleanup_point(self, ctx, ps):
        for b in ps["live"]:
            drop(ctx.suite, b)


class Commit(Verb):
    name = "commit"
    deferred = True

    def prepare_rep(self, ctx, p, ps):
        child = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        start, _ = key_ranges(ctx.rep, p["rows"], stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, child, start, p["rows"])
        return {"child": child}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.commit(rs["child"], "micro commit", timed=timed, label=label)

    def deferred_ref(self, rs):
        return rs["child"]

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["child"])


class Delete(Verb):
    name = "delete"

    def prepare_rep(self, ctx, p, ps):
        child = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        start, start_b = key_ranges(ctx.rep, max(p["written"], p["commits"] * ROWS_PER_HISTORY_COMMIT, 1),
                                    stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, child, start, p["written"])
        commits_of(ctx.suite, child, p["commits"], ROWS_PER_HISTORY_COMMIT, start_b)
        return {"child": child, "deleted": False}

    def run(self, ctx, p, ps, rs, timed, label):
        res = ctx.suite.delete(rs["child"], timed=timed, label=label)
        rs["deleted"] = res.ok
        return res

    def cleanup_rep(self, ctx, rs):
        if not rs["deleted"]:
            drop(ctx.suite, rs["child"])


class Diff(Verb):
    name = "diff"

    def prepare_rep(self, ctx, p, ps):
        child = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        start, _ = key_ranges(ctx.rep, p["rows"], stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, child, start, p["rows"])
        commit(ctx.suite, child, "diff side")
        return {"child": child}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.diff(ctx.point, rs["child"], timed=timed, label=label)

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["child"])


class Merge(Verb):
    name = "merge"

    def prepare_rep(self, ctx, p, ps):
        ours = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        theirs = fork(ctx.suite, f"{ctx.point}b{ctx.rep}", ours)
        start_a, start_b = key_ranges(ctx.rep, p["rows"], p["conflict"], stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, ours, start_a, p["rows"], bump=1)
        commit(ctx.suite, ours, "ours moves on")
        update_rows(ctx.suite, theirs, start_b, p["rows"], bump=2)
        commit(ctx.suite, theirs, "theirs")
        return {"ours": ours, "theirs": theirs}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.merge(rs["ours"], rs["theirs"], "micro merge", on_conflict="ours",
                               timed=timed, label=label)

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["theirs"])
        drop(ctx.suite, rs["ours"])


class Rebase(Verb):
    name = "rebase"

    def prepare_rep(self, ctx, p, ps):
        upstream = fork(ctx.suite, f"{ctx.point}b{ctx.rep}", ctx.point)
        branch = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", upstream)
        start_a, start_b = key_ranges(ctx.rep, max(p["rows"], p["upstream"]), stock_rows=ctx.stock_rows)
        k = max(1, p["commits"])
        commits_of(ctx.suite, branch, k, max(1, p["rows"] // k), start_a)
        update_rows(ctx.suite, upstream, start_b, p["upstream"])
        commit(ctx.suite, upstream, "upstream moves on")
        return {"branch": branch, "upstream": upstream}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.rebase(rs["branch"], rs["upstream"], on_conflict="ours", timed=timed, label=label)

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["branch"])
        drop(ctx.suite, rs["upstream"])


class Reset(Verb):
    name = "reset"

    def prepare_rep(self, ctx, p, ps):
        child = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        start_a, start_b = key_ranges(ctx.rep, max(p["rows"], p["distance"]), stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, child, start_a, p["rows"])
        target = commit(ctx.suite, child, "reset target")
        update_rows(ctx.suite, child, start_b, p["distance"])
        commit(ctx.suite, child, "to discard")
        return {"child": child, "target": target}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.reset(rs["child"], rs["target"], timed=timed, label=label)

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["child"])


class Revert(Verb):
    name = "revert"

    def prepare_rep(self, ctx, p, ps):
        child = fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)
        start_a, start_b = key_ranges(ctx.rep, max(p["rows"], ROWS_PER_HISTORY_COMMIT), stock_rows=ctx.stock_rows)
        update_rows(ctx.suite, child, start_a, ROWS_PER_HISTORY_COMMIT)
        commit(ctx.suite, child, "base")
        update_rows(ctx.suite, child, start_b, p["rows"])
        to_revert = commit(ctx.suite, child, "to revert")
        return {"child": child, "commit": to_revert}

    def run(self, ctx, p, ps, rs, timed, label):
        return ctx.suite.revert(rs["child"], rs["commit"], timed=timed, label=label)

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["child"])


class Control(Verb):
    """No verb: the follow-up writes alone, on a fresh branch. Its storage
    delta is what the deferred deltas of branch and commit are compared
    against."""

    name = "control"
    deferred = True

    def prepare_rep(self, ctx, p, ps):
        return {"child": fork(ctx.suite, f"{ctx.point}a{ctx.rep}", ctx.point)}

    def run(self, ctx, p, ps, rs, timed, label):
        return None

    def deferred_ref(self, rs):
        return rs["child"]

    def cleanup_rep(self, ctx, rs):
        drop(ctx.suite, rs["child"])


VERB_IMPL = {v.name: v for v in (Branch(), Commit(), Delete(), Diff(), Merge(), Rebase(), Reset(), Revert())}
CONTROL = Control()


def follow_up(suite: DBToolSuite, ref: str, rep: int, stock_rows: int = STOCK_ROWS) -> None:
    """The deferred writes: FOLLOW_UP_ROWS updates on ``ref``, on keys the
    rep's own writes did not touch (the upper half of the warehouse)."""
    half = stock_rows // 2
    slots = max(1, (stock_rows - half) // FOLLOW_UP_ROWS)
    start = half + 1 + (rep % slots) * FOLLOW_UP_ROWS
    update_rows(suite, ref, start, min(FOLLOW_UP_ROWS, stock_rows - start + 1))
