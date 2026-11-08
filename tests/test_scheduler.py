# SPDX-License-Identifier: MIT
"""The scheduler's rule (may_start) and the queue's estimates (queue_view),
without root and without a box: python3 -m unittest discover tests"""
import importlib.util
import json
import time
import os
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("mcu", Path(__file__).parent.parent / "box" / "mcu.py")
mcu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcu)

G = 2**30
THREADS, POOL, MAX_WAIT = 10, 14 * G, 1800


def done(job, seconds, project="myapp", status=0):
    return {"kind": "done", "project": project, "job": job, "seconds": seconds, "status": status}


def job(id, name, cores=5, memory=4 * G, project="myapp", **kw):
    return {"id": id, "project": project, "job": name, "cores": cores, "memory": memory,
            "exclusive": [], "queued": kw.pop("queued", time.time()), **kw}


def running(id, name, cores=5, memory=10 * G, started_ago=300, project="myapp", **kw):
    return dict(job(id, name, cores, memory, project, **kw), threads=list(range(cores)),
                started=time.time() - started_ago)


HISTORY = [done("test-affected", 2100), done("test-affected", 2200), done("gpu", 600),
           done("survival", 900), done("lint", 700), done("release", 1500)]


class MayStart(unittest.TestCase):
    def decide(self, me, ahead, run, events=HISTORY, now=None):
        now = now or time.time()
        est = mcu.Estimates(events)
        live = [mcu.run_slot(r, now, est) for r in run]
        return mcu.may_start(me, ahead, live, now, est, THREADS, POOL, MAX_WAIT)

    def test_backfill_beside_a_big_job(self):
        # today's queue: a 10 GB test runs, a 10 GB test waits first, a 4 GB
        # gpu job behind it fits now and ends before the test can start
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "test-affected", memory=10 * G)]
        self.assertIsNone(self.decide(job("g", "gpu"), ahead, run))

    def test_no_backfill_when_it_would_delay(self):
        # the small job lasts longer than the running test has left, and
        # the 6 threads the job ahead needs are not free beside it
        run = [running("r", "test-affected", started_ago=2000)]
        ahead = [job("a", "test-affected", cores=6, memory=10 * G)]
        why = self.decide(job("s", "survival"), ahead, run)
        self.assertTrue(why.startswith("behind myapp/test-affected"), why)

    def test_unknown_duration_uses_the_timeout(self):
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "test-affected", cores=6, memory=10 * G)]
        self.assertIsNone(self.decide(job("n", "new", timeout=600), ahead, run))
        self.assertIsNotNone(self.decide(job("n", "new", timeout=7200), ahead, run))
        self.assertIsNotNone(self.decide(job("n", "new"), ahead, run))

    def test_leaves_room_at_the_reserved_start(self):
        # a 2-thread, 1 GB job that never ends still leaves room for the
        # 6-thread job ahead once the running one ends
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "lint", cores=6, memory=3 * G)]
        self.assertIsNone(self.decide(job("x", "x", cores=2, memory=G), ahead, run))

    def test_does_not_take_what_fits_now(self):
        # the job ahead fits now: whatever passes it must leave its room
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "lint", memory=3 * G)]
        self.assertIsNotNone(self.decide(job("x", "x", cores=2, memory=G), ahead, run))

    def test_fits_beside_the_job_ahead(self):
        # today's case: 4 GB beside a 10 GB test, then still beside the next
        # 10 GB test (14 GB in all): it delays no one, whatever its length
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "test-affected", memory=10 * G)]
        self.assertIsNone(self.decide(job("n", "new", cores=2), ahead, run))

    def test_max_wait(self):
        run = [running("r", "test-affected", started_ago=600)]
        old = job("a", "test-affected", cores=6, memory=10 * G, queued=time.time() - 3600)
        why = self.decide(job("g", "gpu"), [old], run)
        self.assertIn("not overtaken", why)
        # its own max_wait, longer
        old["max_wait"] = 7200
        self.assertIsNone(self.decide(job("g", "gpu"), [old], run))

    def test_nothing_passes_a_machine_job(self):
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "bench", exclusive=["machine"])]
        self.assertIn("needs the machine", self.decide(job("g", "gpu"), ahead, run))

    def test_machine_job_waits_for_an_empty_box(self):
        run = [running("r", "gpu", cores=2, memory=G)]
        self.assertIn("whole machine", self.decide(job("b", "bench", exclusive=["machine"]), [], run))
        self.assertIsNone(self.decide(job("b", "bench", exclusive=["machine"]), [], []))

    def test_same_cache_keeps_its_order(self):
        run = [running("r", "test-affected", started_ago=600)]
        ahead = [job("a", "test-affected", memory=10 * G, locks=["build"])]
        why = self.decide(job("g", "gpu", locks=["build"]), ahead, run)
        self.assertIn("same cache", why)

    def test_resources(self):
        run = [running("r", "test-affected")]
        self.assertIn("GB of memory", self.decide(job("t", "test-affected", memory=10 * G), [], run))
        self.assertIn("threads", self.decide(job("t", "x", cores=6), [], run))

    def test_cancelled_runs_are_not_durations(self):
        est = mcu.Estimates([done("gpu", 600), done("gpu", 5, status=mcu.EX_CANCELLED)])
        self.assertEqual(est.typical({"project": "myapp", "job": "gpu"}), 600)


class QueueView(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(mcu, k) for k in ("job_threads", "job_memory", "hold", "maintenance", "box_conf")}
        mcu.job_threads = lambda: list(range(THREADS))
        mcu.job_memory = lambda: POOL
        mcu.hold = mcu.maintenance = lambda: None
        mcu.box_conf = lambda: {}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(mcu, k, v)

    def test_backfilled_job_starts_now(self):
        now = time.time()
        sched = {"running": [running("r", "test-affected", started_ago=600)],
                 "queue": [job("a", "test-affected", memory=10 * G), job("g", "gpu")]}
        v = mcu.queue_view(sched, HISTORY)
        self.assertAlmostEqual(v["waiting"]["g"]["eta_start"], now, delta=5)
        # the test waits for the running one: its median (2200 s) minus 600
        self.assertAlmostEqual(v["waiting"]["a"]["eta_start"] - now, 2200 - 600, delta=5)
        self.assertEqual(v["waiting"]["g"]["position"], 2)

    def test_unknown_history_unknown_times(self):
        sched = {"running": [running("r", "new", started_ago=60)],
                 "queue": [job("a", "test-affected", memory=10 * G)]}
        v = mcu.queue_view(sched, HISTORY)
        self.assertIsNone(v["ends"]["r"])
        self.assertIsNone(v["waiting"]["a"]["eta_start"])

    def test_high_priority_goes_first(self):
        now = time.time()
        sched = {"running": [running("r", "test-affected", started_ago=600)],
                 "queue": [job("a", "test-affected", cores=6, memory=10 * G),
                           job("h", "release", cores=6, memory=10 * G, priority="high")]}
        v = mcu.queue_view(sched, HISTORY)
        self.assertEqual(v["waiting"]["h"]["position"], 1)
        self.assertAlmostEqual(v["waiting"]["h"]["eta_start"] - now, 2200 - 600, delta=5)
        self.assertGreater(v["waiting"]["a"]["eta_start"], v["waiting"]["h"]["eta_start"])

    def test_priority_levels_within_a_project(self):
        ids = lambda q: [e["id"] for e in mcu.queue_order(q)]
        o = lambda i, **kw: job(i, "j", project="other", **kw)
        # myapp's places (1, 3, 4) are dealt again: high, normal, low; other's jobs keep theirs
        q = [job("h1", "j", priority="low"), o("o1"), job("h2", "j"), o("o2"), job("h3", "j", priority="high")]
        self.assertEqual(ids(q), ["h3", "o1", "h2", "o2", "h1"])
        # several high jobs: only the first of each project goes ahead of the other projects
        # (other's o3 first takes other's first place, so it is ahead of h1)
        q = [o("o1"), o("o2"), job("h1", "j", priority="high"), job("h2", "j", priority="high"),
             o("o3", priority="high")]
        self.assertEqual(ids(q), ["o3", "h1", "o1", "h2", "o2"])
        # no priority: queue order
        q = [o("o1"), job("h1", "j"), o("o2")]
        self.assertEqual(ids(q), ["o1", "h1", "o2"])

    def test_held_queue(self):
        mcu.hold = lambda: "admin"
        v = mcu.queue_view({"running": [], "queue": [job("a", "gpu")]}, HISTORY)
        self.assertIsNone(v["waiting"]["a"]["eta_start"])


class Git(unittest.TestCase):
    def test_maintenance_never_detaches(self):
        # a detached auto maintenance outlives the cache's lock and races the next job's clone
        import subprocess
        calls = []
        saved = subprocess.run
        subprocess.run = lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
        try:
            mcu.git("fetch", "--quiet", "origin", cwd="/tmp")
        finally:
            subprocess.run = saved
        cmd = calls[0]
        for flag in ("maintenance.autoDetach=false", "gc.autoDetach=false"):
            self.assertEqual(cmd[cmd.index(flag) - 1], "-c")
        self.assertLess(cmd.index("gc.autoDetach=false"), cmd.index("fetch"))


class Names(unittest.TestCase):
    def test_project_from_user(self):
        saved = os.environ.get("USER")
        try:
            for user, project in (("mcu-myapp", "myapp"), ("bench-myapp", "myapp")):
                os.environ["USER"] = user
                self.assertEqual(mcu.project_name(), project)
            for user in ("mcu-web", "bench-web", "alice"):
                os.environ["USER"] = user
                with self.assertRaises(mcu.Refused):
                    mcu.project_name()
        finally:
            os.environ["USER"] = saved or ""


class Docs(unittest.TestCase):
    def test_sections_skip_code_and_stop_at_their_level(self):
        text = ("# Guide\n## 1. A\n<!-- topic: a alias -->\nalpha\n```sh\n# not a heading\n```\n"
                "### 1.1 sub\n<!-- topic: sub -->\nsub text\n## 2. B\n<!-- topic: b -->\nbeta\n")
        s = mcu.doc_sections(text)
        self.assertEqual(sorted(s), ["a", "alias", "b", "sub"])
        self.assertIn("# not a heading", s["a"][1])
        self.assertIn("sub text", s["a"][1])          # a section holds its sub-sections
        self.assertNotIn("beta", s["a"][1])
        self.assertNotIn("<!--", s["a"][1])
        self.assertEqual(s["sub"][0], "1.1 sub")

    def test_every_topic_the_box_names_exists(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        named = set(re.findall(r'topic="([a-z]+)"', (root / "box" / "mcu.py").read_text()))
        named |= set(re.findall(r"see: docs ([a-z]+)", (root / "box" / "mcu.py").read_text()))
        named |= {"agents", "view"}
        sections = mcu.doc_sections((root / "docs" / "USING.md").read_text())
        self.assertEqual(sorted(named - set(sections)), [])

    def test_refused_names_its_topic(self):
        self.assertEqual(str(mcu.Refused("no", topic="storage")), "no; see: docs storage")
        self.assertEqual(str(mcu.Refused("no")), "no")


class Latest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(mcu.latest_names("lint", {}), ["lint"])
        self.assertEqual(mcu.latest_names("release", {"branch": "wip/x", "a": 1}),
                         ["release", "release,a=1,branch=wip%2Fx"])

    def test_links_follow_the_last_success(self):
        import tempfile
        from pathlib import Path
        jobs = Path(tempfile.mkdtemp())
        for i in ("20261029-100000-aaaa", "20261029-110000-bbbb"):
            (jobs / i).mkdir()
            mcu.link_latest(jobs / i, {"name": "release", "params": {"branch": "main"}})
        latest = jobs / ".latest"
        self.assertEqual(os.readlink(latest / "release"), "../20261029-110000-bbbb")
        self.assertEqual((latest / "release,branch=main").resolve(), (jobs / "20261029-110000-bbbb").resolve())
        # a run the retention removed: its link goes at the next success
        import shutil
        shutil.rmtree(jobs / "20261029-110000-bbbb")
        (jobs / "20261029-120000-cccc").mkdir()
        mcu.link_latest(jobs / "20261029-120000-cccc", {"name": "lint", "params": {}})
        self.assertEqual(sorted(p.name for p in latest.iterdir()), ["lint"])


class MemoryHit(unittest.TestCase):
    """swap is a sign of the job's limit only near that limit: the box's own
    pressure swaps a little of any job out"""

    def run_hit(self, anon, swapped, code=0, oom=0):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        out = d / "out"
        out.mkdir()
        (out / ".bench-trace").write_text(f"1 0 {anon} {anon}\n")
        (out / ".bench-swap-peak").write_text(f"{swapped}\n")
        (out / ".bench-memory-events").write_text(f"oom_kill {oom}\n")
        logs = []
        grow = mcu.memory_hit(d, code, {"memory": "12g", "memory_need": 10 * G}, logs.append)
        return grow, logs

    def test_swap_far_below_the_limit_is_not_a_hit(self):
        grow, logs = self.run_hit(2 * G, 200 * 2**20)
        self.assertIsNone(grow)
        self.assertIn("box's memory pressure", logs[0])

    def test_swap_near_the_limit_is_a_hit(self):
        grow, _ = self.run_hit(11 * G, 200 * 2**20)
        self.assertEqual(grow, 18 * G)

    def test_a_kill_is_a_hit(self):
        self.assertEqual(self.run_hit(2 * G, 0, code=137, oom=1)[0], 18 * G)
        self.assertEqual(self.run_hit(11 * G, 0, code=137)[0], 18 * G)
        self.assertIsNone(self.run_hit(2 * G, 0, code=137)[0])


class MemoryPeak(unittest.TestCase):
    """the memory line: the need counts what went to swap, and the line says
    what each figure is"""

    def trace(self, text):
        import tempfile
        from pathlib import Path
        out = Path(tempfile.mkdtemp())
        (out / ".bench-trace").write_text(text)
        return out

    def test_need_counts_swap(self):
        out = self.trace(f"1 0 {5 * G} {2 * G} 0 {G}\n2 0 {5 * G} {2 * G} {G // 2} {3 * G}\n3 0 {G} {G} 0 0\n")
        p = mcu.trace_peaks(out)
        self.assertEqual(p, {"need": 2 * G + G // 2, "anon": 2 * G, "swap": G // 2, "mapped": 3 * G})
        self.assertEqual(mcu.anon_peak(out), 2 * G + G // 2)

    def test_old_trace(self):
        out = self.trace(f"1 0 {5 * G} {2 * G}\n1 0 {5 * G}\n")
        self.assertEqual(mcu.trace_peaks(out), {"need": 2 * G, "anon": 2 * G, "swap": 0, "mapped": 0})
        self.assertIsNone(mcu.anon_peak(self.trace("")))

    def test_line(self):
        m = {"need_peak": int(2.4 * G), "cgroup_peak": 5 * G, "mapped_peak": int(1.8 * G),
             "swap_peak": int(0.2 * G), "sample_seconds": 1}
        self.assertEqual(mcu.memory_line(m["need_peak"], m),
                         "memory peak 2.40 GB anonymous incl. swap (what the job needs; sampled every 1 s); "
                         "cgroup peak 5.00 GB with page cache, 1.80 GB mapped files at most, swap peak 0.20 GB")
        self.assertEqual(mcu.memory_line(2 * G, None, 3 * G), "memory peak 2.00 GB; the next run is sized for 3.00 GB")


class Seeds(unittest.TestCase):
    """a new sub starts from its seed, or from the seed's snapshot while a
    job writes the seed; room is made for the copy by evicting old subs"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cache = self.home / "cache" / "target"
        (self.cache / "main").mkdir(parents=True)
        (self.cache / "main" / "lib.rlib").write_text("main now")
        (self.cache / ".bench-per").touch()
        (self.cache / ".bench-tmp").touch()
        self.state, self.media = {"running": []}, {}
        saved = {k: getattr(mcu, k) for k in ("read_state_json", "disk_media", "project_name", "seed_subs", "event")}
        self.events = []
        self.addCleanup(lambda: [setattr(mcu, k, v) for k, v in saved.items()])
        self.addCleanup(self.tmp.cleanup)
        mcu.read_state_json = lambda name: self.state if name == "state.json" else {}
        mcu.disk_media = lambda project: self.media
        mcu.project_name = lambda: "p"
        mcu.seed_subs = lambda project: {"target/main"}
        mcu.event = lambda **e: self.events.append(e)
        self.saved_home = mcu.Path.home
        mcu.Path.home = classmethod(lambda cls: self.home)
        self.addCleanup(lambda: setattr(mcu.Path, "home", self.saved_home))

    def seed(self, sub):
        logs = []
        (self.cache / sub).mkdir()
        mcu.seed_sub("target", sub, "main", logs.append)
        return logs

    def test_idle_seed_is_copied(self):
        self.seed("feat_x")
        self.assertEqual((self.cache / "feat_x" / "lib.rlib").read_text(), "main now")

    def test_busy_seed_copies_its_snapshot(self):
        (self.cache / ".seed-main").mkdir()
        (self.cache / ".seed-main" / "lib.rlib").write_text("main at the snapshot")
        self.state = {"running": [{"project": "p", "job": "release", "subs": ["target/main"]}]}
        logs = self.seed("feat_x")
        self.assertEqual((self.cache / "feat_x" / "lib.rlib").read_text(), "main at the snapshot")
        self.assertIn("copying its snapshot", logs[0])

    def test_busy_seed_without_snapshot_starts_empty(self):
        self.state = {"running": [{"project": "p", "job": "release", "subs": ["target/main"]}]}
        logs = self.seed("feat_x")
        self.assertEqual(list((self.cache / "feat_x").iterdir()), [])
        self.assertIn("no snapshot yet", logs[0])

    def test_room_is_made_by_evicting_old_subs(self):
        import os
        old = self.cache / "feat_old"
        old.mkdir()
        (old / "big").write_bytes(b"x" * 8192)
        os.utime(old, (1, 1))
        size = mcu.du(self.cache / "main")
        # the copy fits only once feat_old is gone; the snapshot and the
        # seed are never evicted
        (self.cache / ".seed-main").mkdir()
        self.media = {"ssd": {"used": 100_000, "quota": 100_000 + size - 1, "mounts": ["/"]}}
        logs = self.seed("feat_x")
        self.assertFalse(old.exists())
        self.assertTrue((self.cache / ".seed-main").exists())
        self.assertEqual((self.cache / "feat_x" / "lib.rlib").read_text(), "main now")
        self.assertTrue(any("evicted storage target/feat_old" in line for line in logs))

    def test_snapshot_after_a_job(self):
        logs = []
        mcu.snapshot_seeds("p", ["target/main", "target/feat_x"], logs.append)
        snap = self.cache / ".seed-main"
        # a pool without reflink takes none (a full copy would cost the
        # seed's size on disk); either way nothing half-made is left
        if snap.exists():
            self.assertEqual((snap / "lib.rlib").read_text(), "main now")
            self.assertIn("seed snapshot taken", logs[-1])
        else:
            self.assertIn("no seed snapshot taken", logs[-1])
        self.assertEqual(sorted(p.name for p in self.cache.iterdir() if p.name.startswith(".seed-")),
                         [".seed-main"] if snap.exists() else [])
        # not a seed: no snapshot
        self.assertFalse((self.cache / ".seed-feat_x").exists())

    def test_snapshots_are_not_evictable(self):
        (self.cache / ".seed-main").mkdir()
        (self.cache / "feat_y").mkdir()
        names = [n for _, n, _ in mcu.tmp_subs("p")]
        self.assertEqual(names, ["target/feat_y"])


class First(unittest.TestCase):
    """first = [...]: a job's prerequisites from its Benchfile, submitted
    with it (or the identical one already in flight), reused per commit"""

    MANIFEST = {"jobs": {"check": {"command": "c"},
                         "test": {"command": "t", "first": [{"job": "check", "pass": ["branch"]}]}}}

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name)
        names = ("jobs_dir", "project_conf", "project_name", "pick_benchfile", "load_toml", "create_job",
                 "recent_events")
        saved = {k: getattr(mcu, k) for k in names}
        self.addCleanup(lambda: [setattr(mcu, k, v) for k, v in saved.items()])
        mcu.jobs_dir = lambda: self.jobs
        mcu.project_name = lambda: "myapp"
        mcu.project_conf = lambda p: {"benchfile": 3}
        mcu.pick_benchfile = lambda conf, params: (None, 3)
        mcu.load_toml = lambda path: self.MANIFEST
        self.created = []

        def create_job(kind, branch, name, params=None, deps=None, ext=None, prio=None, extra=None):
            d = self.jobs / f"20261108-1000{len(self.created):02d}-abcd"
            d.mkdir()
            (d / "request").write_text(json.dumps({"kind": kind, "name": name, "params": params or {}, **(extra or {})}))
            self.created.append((name, params, extra))
            return d
        mcu.create_job = create_job

    def test_check_first(self):
        ok = {"check": {}, "test": {"first": [{"job": "check", "pass": ["branch"]}]}}
        said = []
        mcu.check_first(ok, said.append)
        self.assertIn("job test: first check (passes branch)", said[0])
        for bad in ([{"job": "nope"}], [{"job": "test"}], [{"job": "check", "pass": ["Bad-Name"]}],
                    [{"job": "check", "when": "x"}], "check", [{"job": ["check"]}]):
            with self.assertRaises(mcu.Refused, msg=repr(bad)):
                mcu.check_first({"check": {}, "test": {"first": bad}})
        with self.assertRaises(mcu.Refused):   # one level only
            mcu.check_first({"lint": {}, "check": {"first": [{"job": "lint"}]}, "test": {"first": [{"job": "check"}]}})

    def test_first_of(self):
        self.assertEqual(mcu.first_of("test", {"branch": "x", "crate": "c"}), [("check", {"branch": "x"})])
        self.assertEqual(mcu.first_of("check", {"branch": "x"}), [])

    def test_submit_first_then_attach(self):
        deps = {"after": [], "after_ok": []}
        extra = mcu.submit_first("test", {"branch": "x"}, deps, None)
        self.assertEqual(self.created, [("check", {"branch": "x"}, {"prereq": True})])
        pre = extra["first"][0]
        self.assertEqual(deps["after_ok"], [pre])
        # a second job while that check is still queued: it waits on the same one
        deps2 = {"after": [], "after_ok": []}
        self.assertEqual(mcu.submit_first("test", {"branch": "x"}, deps2, None), {"first": [pre]})
        self.assertEqual(len(self.created), 1)
        # once it ended, a new one is submitted (it may reuse the result by commit)
        (self.jobs / pre / "status").write_text("exit 0\n")
        mcu.submit_first("test", {"branch": "x"}, {"after_ok": []}, None)
        self.assertEqual(len(self.created), 2)

    def test_first_none(self):
        params, deps = {"branch": "x", "first": "none"}, {"after_ok": []}
        self.assertEqual(mcu.submit_first("test", params, deps, None), {})
        self.assertEqual((params, deps, self.created), ({"branch": "x"}, {"after_ok": []}, []))

    def test_passed_before(self):
        done = lambda i, **kw: dict({"kind": "done", "id": i, "project": "myapp", "job": "check", "status": 0,
                                     "params": {"branch": "x"}, "commits": {"app": "abc"}, "benchfile": 3}, **kw)
        mcu.recent_events = lambda: [done("a"), done("b", status=101), done("c", commits={"app": "def"}),
                                     done("d", reused="a")]
        find = lambda commits, **kw: mcu.passed_before("myapp", "check", kw.get("params", {"branch": "x"}),
                                                       commits, kw.get("bf", (None, 3)), kw.get("pushed", ""))
        self.assertEqual(find({"app": "abc"}), "a")        # the run that really passed, through its reuse
        self.assertEqual(find({"app": "def"}), "c")
        self.assertIsNone(find({"app": "fff"}))             # another commit
        self.assertIsNone(find({"app": "abc"}, params={"branch": "y"}))
        self.assertIsNone(find({"app": "abc"}, bf=(None, 4)))     # another Benchfile version
        self.assertIsNone(find({"app": "abc"}, pushed="1234"))    # other pushed code
        self.assertIsNone(find({}))                          # no checkout: nothing to compare

    def test_a_failed_first_names_it(self):
        pre = self.jobs / "20261108-090000-aaaa"
        pre.mkdir()
        (pre / "request").write_text(json.dumps({"name": "check"}))
        (pre / "status").write_text("exit 101\n")

        class Sched:
            def block(self, e): pass
            def waits_for(self, i, dep): pass
            def release(self, i): pass

        class Finished(Exception):
            pass

        def finish(code, **extra):
            raise Finished(code, extra)
        logs = []
        with self.assertRaises(Finished) as cm:
            mcu.wait_deps("20261108-100000-cccc", "myapp", {"name": "test", "after_ok": [pre.name], "first": [pre.name]},
                          logs.append, finish, Sched())
        self.assertEqual(cm.exception.args[0], mcu.EX_DEPENDENCY)
        self.assertIn("check 20261108-090000-aaaa failed (exit 101)", cm.exception.args[1]["reason"])
        self.assertTrue(any(line.startswith("first: check") for line in logs))


class Dependencies(unittest.TestCase):
    """after= / after_ok=, x:<ext_job_id>, chains: in a temporary jobs dir"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.tmp.name)
        self.saved = {k: getattr(mcu, k) for k in
                      ("jobs_dir", "read_state_json", "box_conf", "project_name", "create_job", "follow",
                       "submit_warnings", "time")}
        mcu.jobs_dir = lambda: self.jobs
        mcu.read_state_json = lambda name: {}
        mcu.box_conf = lambda: {}
        mcu.project_name = lambda: "myapp"
        self.created = []

        def create_job(kind, branch, name, params=None, deps=None, ext=None, prio=None):
            d = self.make(f"20261028-1100{len(self.created):02d}-abcd")
            self.created.append((name, params, deps, ext, d.name))
            return d
        mcu.create_job = create_job
        mcu.submit_warnings = lambda: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(mcu, k, v)
        self.tmp.cleanup()

    def make(self, job_id, status=None, ext=None):
        d = self.jobs / job_id
        d.mkdir()
        if status is not None:
            (d / "status").write_text(f"exit {status}\n")
        if ext:
            (self.jobs / ".ext").mkdir(exist_ok=True)
            link = self.jobs / ".ext" / ext
            link.unlink(missing_ok=True)
            link.symlink_to(f"../{job_id}")
        return d

    def test_take_deps(self):
        rest, deps, ext, prio = mcu.take_deps(["branch=x", "after=20261028-100000-aaaa,x:lint-x",
                                           "after_ok=20261028-100000-bbbb", "ext_job_id=test-x"])
        self.assertEqual(rest, ["branch=x"])
        self.assertEqual(deps, {"after": ["20261028-100000-aaaa", "x:lint-x"], "after_ok": ["20261028-100000-bbbb"]})
        self.assertEqual(ext, "test-x")
        self.assertEqual(mcu.take_deps(["priority=high"])[3], "high")
        self.assertEqual(mcu.take_deps(["priority=low"])[3], "low")
        with self.assertRaises(mcu.Refused):
            mcu.take_deps(["priority=urgent"])
        with self.assertRaises(mcu.Refused):
            mcu.take_deps(["after=../etc"])
        with self.assertRaises(mcu.Refused):
            mcu.take_deps(["ext_job_id=a/b"])

    def test_refs(self):
        self.make("20261028-100000-aaaa", 1, ext="lint-x")
        self.make("20261028-100001-aaaa", ext="lint-x")   # resubmitted: takes the name
        self.assertEqual(mcu.resolve_ref("x:lint-x"), "20261028-100001-aaaa")
        self.assertEqual(mcu.job_dir("x:lint-x").name, "20261028-100001-aaaa")
        self.assertIsNone(mcu.resolve_ref("lint-x"))     # bare: a box id only
        with self.assertRaises(mcu.Refused):
            mcu.job_dir("x:nope")

    def test_check_deps(self):
        self.make("20261028-100000-aaaa", 0)
        self.make("20261028-100001-aaaa", 2)
        self.make("20261028-100002-aaaa")
        notes = mcu.check_deps("myapp", {"after": ["20261028-100001-aaaa"],
                                            "after_ok": ["20261028-100000-aaaa", "20261028-100002-aaaa"]})
        self.assertEqual(len(notes), 2)          # two already ended: met
        with self.assertRaises(mcu.Refused):   # failed, and after_ok wants success
            mcu.check_deps("myapp", {"after": [], "after_ok": ["20261028-100001-aaaa"]})
        with self.assertRaises(mcu.Refused):   # unknown here (another project's, or none)
            mcu.check_deps("myapp", {"after": ["20261028-999999-ffff"], "after_ok": []})

    def test_limit(self):
        self.make("20261028-100002-aaaa")
        mcu.box_conf = lambda: {"max_waiting_deps": 1}
        mcu.read_state_json = lambda name: {"deps": [{"project": "myapp"}]}
        with self.assertRaises(mcu.Refused):
            mcu.check_deps("myapp", {"after": ["20261028-100002-aaaa"], "after_ok": []})

    def wait(self, req):
        """wait_deps with a stand-in scheduler; the code finish() got, or None when met"""
        class Sched:
            def block(self, e): pass
            def waits_for(self, i, dep): pass
            def release(self, i): pass

        class Finished(Exception):
            pass

        def finish(code, **extra):
            raise Finished(code)
        try:
            mcu.wait_deps("20261028-110000-cccc", "myapp", dict(req, name="j"), lambda m: None, finish, Sched())
        except Finished as e:
            return e.args[0]
        return None

    def test_wait_met_and_failed(self):
        self.make("20261028-100000-aaaa", 0)
        self.make("20261028-100001-aaaa", 1)
        self.assertIsNone(self.wait({"after_ok": ["20261028-100000-aaaa"], "after": ["20261028-100001-aaaa"]}))
        self.assertEqual(self.wait({"after_ok": ["20261028-100001-aaaa"]}), mcu.EX_DEPENDENCY)
        self.make("20261028-100002-aaaa", mcu.EX_CANCELLED)
        self.assertEqual(self.wait({"after_ok": ["20261028-100002-aaaa"]}), mcu.EX_DEPENDENCY)

    def test_wait_follows_a_resubmitted_ext_job(self):
        # the failed job's name now carries a successful rerun
        self.make("20261028-100000-aaaa", 1, ext="gate-x")
        self.assertEqual(self.wait({"after_ok": ["x:gate-x"]}), mcu.EX_DEPENDENCY)
        self.make("20261028-100001-aaaa", 0, ext="gate-x")
        self.assertIsNone(self.wait({"after_ok": ["x:gate-x"]}))

    def test_chain(self):
        mcu.follow = lambda d: 0
        code = mcu.chain(["# one feature branch", "lint = lint branch=x ext_job_id=lint-x",
                            "test = test-affected branch=x",
                            "release = release branch=x after_ok=lint,test"])
        self.assertEqual(code, 0)
        names = [c[0] for c in self.created]
        self.assertEqual(names, ["lint", "test-affected", "release"])
        # fan-in: the release waits for both; lint by its ext id, test by its job id
        self.assertEqual(self.created[2][2]["after_ok"], ["x:lint-x", self.created[1][4]])

    def test_bump_changes_only_that_job(self):
        import json
        import os
        saved = mcu.STATE
        mcu.STATE = self.jobs
        try:
            for i in ("20261028-100000-aaaa", "20261028-100001-aaaa"):
                self.make(i)
                (self.jobs / i / "request").write_text(json.dumps({"name": "j"}))
            q = lambda i, **kw: dict(job(i, "j", pid=os.getpid()), **kw)
            (self.jobs / "state.json").write_text(json.dumps({"queue": [
                q("20261028-100000-aaaa", priority="high"), q("20261028-100001-aaaa"),
                q("20261028-100002-bbbb", project="other", priority="high")], "running": []}))
            self.assertTrue(mcu.Scheduler().prioritize("myapp", "20261028-100001-aaaa"))
            state = json.loads((self.jobs / "state.json").read_text())
            self.assertEqual([e.get("priority") for e in state["queue"]], ["high", "high", "high"])
            self.assertEqual(json.loads((self.jobs / "20261028-100001-aaaa" / "request").read_text())["priority"], "high")
            self.assertTrue(mcu.Scheduler().prioritize("myapp", "20261028-100000-aaaa", "normal"))
            state = json.loads((self.jobs / "state.json").read_text())
            self.assertEqual([e.get("priority") for e in state["queue"]], [None, "high", "high"])
            self.assertNotIn("priority", json.loads((self.jobs / "20261028-100000-aaaa" / "request").read_text()))
            # another project's job is not this project's to bump
            self.assertFalse(mcu.Scheduler().prioritize("myapp", "20261028-100002-bbbb", "low"))
            self.assertFalse(mcu.Scheduler().prioritize("myapp", "20261028-999999-cccc"))
        finally:
            mcu.STATE = saved

    def test_chain_refused_before_anything(self):
        for lines in (["a = lint after_ok=b", "b = test"],          # not an earlier step
                      ["a = lint", "a = test"],                     # named twice
                      ["lint branch=x"],                            # no name
                      ["a = Bad_Name"]):
            with self.assertRaises(mcu.Refused, msg=lines):
                mcu.chain(lines)
            self.assertEqual(self.created, [])


class StorageLife(unittest.TestCase):
    """a storage's life record lives outside it: a service's container may
    own the storage (it chowns its data directory) and the runner, as the
    project user, could no longer write inside"""

    def setUp(self):
        import tempfile
        self.home = Path(tempfile.mkdtemp())
        (self.home / "cache" / "db").mkdir(parents=True)
        (self.home / "cache" / "build" / "main").mkdir(parents=True)

    def tearDown(self):
        import shutil
        for d in self.home.rglob("*"):
            if d.is_dir():
                d.chmod(0o755)
        shutil.rmtree(self.home)

    def test_where_the_record_lives(self):
        self.assertEqual(mcu.life_file(self.home / "cache" / "db"), self.home / ".mcu-life" / "db.json")
        self.assertEqual(mcu.life_file(self.home / "cache" / "build" / "main"),
                         self.home / ".mcu-life" / "build~main.json")

    def test_a_storage_it_cannot_write_into(self):
        db = self.home / "cache" / "db"
        db.chmod(0o555)   # as after a container took it over
        record = mcu.life_file(db)
        record.parent.mkdir(exist_ok=True)
        record.write_text('{"life": {"keep": true}, "used": 1}')
        self.assertEqual(mcu.life_info(db)["life"], {"keep": True})

    def test_an_older_record_inside_is_still_read(self):
        sub = self.home / "cache" / "build" / "main"
        (sub / ".bench-life").write_text('{"life": {"ttl": 86400}}')
        self.assertEqual(mcu.life_info(sub)["life"], {"ttl": 86400})
        self.assertEqual(mcu.life_info(self.home / "cache" / "db"), {})


class Throttled(unittest.TestCase):
    """a run is flagged on the time it lost to throttling, not on event
    counts (thousands of 1 ms throttles cost nothing measurable)"""

    def test_brief_throttles_are_not_a_flag(self):
        start = {"package": 1000, "cores": {1: 0, 2: 0, 7: 0}}
        end = {"package": 1800, "cores": {1: 300, 2: 100, 7: 5000}}   # cpu 7 is not the job's
        secs, share = mcu.throttled(start, end, 436, [1, 2])
        self.assertEqual(secs, 0.8)   # the longer of the package's 0.8 s and its most throttled core's 0.3 s
        self.assertLess(share, mcu.THROTTLE_SHARE)

    def test_a_long_throttle_is(self):
        secs, share = mcu.throttled({"package": 0, "cores": {}}, {"package": 30000, "cores": {}}, 600, [1])
        self.assertEqual((secs, share), (30.0, 0.05))
        self.assertGreater(share, mcu.THROTTLE_SHARE)

    def test_unknown_counters(self):
        self.assertEqual(mcu.throttled(None, None, 10, [1]), (None, None))
        self.assertEqual(mcu.throttled({"package": None, "cores": {}}, {"package": None, "cores": {}}, 10, [1]),
                         (None, None))


class QuotaLines(unittest.TestCase):
    """config usage's quota: every pool type the project has, not only the ssd"""

    def test_one_pool(self):
        self.assertEqual(mcu.quota_lines({"used": 30 * G, "quota": 60 * G}),
                         ["quota       30.00 GB used of  60.00 GB (50%, measured every 5 min)"])

    def test_two_pools(self):
        u = {"used": 30 * G, "quota": 60 * G,
             "media": {"hdd": {"used": 136 * G, "quota": 202 * G}, "ssd": {"used": 30 * G, "quota": 60 * G}}}
        self.assertEqual(mcu.quota_lines(u),
                         ["quota ssd   30.00 GB used of  60.00 GB (50%, measured every 5 min)",
                          "quota hdd  136.00 GB used of 202.00 GB (67%, measured every 5 min)"])

    def test_no_quota(self):
        self.assertEqual(mcu.quota_lines({}), [])
        # a pool type without a quota is not a line of its own
        self.assertEqual(len(mcu.quota_lines({"used": G, "quota": 2 * G, "media": {"ssd": {"used": G, "quota": 2 * G},
                                                                                   "hdd": {"used": 0, "quota": 0}}})), 1)


if __name__ == "__main__":
    unittest.main()
