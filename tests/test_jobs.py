import time

from app.core.jobs import Recurring, RetryLater, Worker, enqueue


def job(conn, kind):
    return conn.execute("SELECT * FROM jobs WHERE kind = ? ORDER BY id DESC", (kind,)).fetchone()


def make_worker(settings, handlers, recurring=()):
    w = Worker(settings.db_path, handlers, list(recurring))
    w.start()
    return w


def test_enqueue_dedupes_active_jobs(conn):
    assert enqueue(conn, "t.a", dedupe_key="k")
    assert not enqueue(conn, "t.a", dedupe_key="k")
    conn.execute("UPDATE jobs SET status = 'done'")
    assert enqueue(conn, "t.a", dedupe_key="k")


def test_success_commits_handler_writes(conn, settings):
    def handler(c, payload):
        c.execute("CREATE TABLE IF NOT EXISTS out (v)")
        c.execute("INSERT INTO out VALUES (?)", (payload["v"],))

    enqueue(conn, "t.write", {"v": 7})
    conn.commit()
    w = make_worker(settings, {"t.write": handler})
    assert w.run_once()
    assert not w.run_once()
    assert job(conn, "t.write")["status"] == "done"
    assert conn.execute("SELECT v FROM out").fetchone()[0] == 7


def test_failure_rolls_back_and_backs_off(conn, settings):
    def handler(c, payload):
        c.execute("CREATE TABLE IF NOT EXISTS out (v)")
        c.execute("INSERT INTO out VALUES (1)")
        raise ValueError("boom")

    enqueue(conn, "t.fail", max_attempts=2)
    conn.commit()
    w = make_worker(settings, {"t.fail": handler})
    w.run_once()
    j = job(conn, "t.fail")
    assert (j["status"], j["attempts"]) == ("pending", 1)
    assert j["run_after"] > time.time() + 30
    assert "boom" in j["last_error"]
    conn.execute("UPDATE jobs SET run_after = 0")
    conn.commit()
    w.run_once()
    assert job(conn, "t.fail")["status"] == "failed"


def test_retry_later_does_not_count_attempt(conn, settings):
    def handler(c, payload):
        raise RetryLater(120)

    enqueue(conn, "t.later")
    conn.commit()
    make_worker(settings, {"t.later": handler}).run_once()
    j = job(conn, "t.later")
    assert (j["status"], j["attempts"]) == ("pending", 0)
    assert j["run_after"] > time.time() + 100


def test_start_resets_interrupted_and_seeds_recurring_once(conn, settings):
    enqueue(conn, "t.x")
    conn.execute("UPDATE jobs SET status = 'running'")
    conn.commit()
    rec = [Recurring("t.tick", 60)]
    make_worker(settings, {}, rec)
    make_worker(settings, {}, rec)  # restart doesn't duplicate
    assert job(conn, "t.x")["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 't.tick'").fetchone()[0] == 1


def test_recurring_reschedules_after_run(conn, settings):
    w = make_worker(settings, {"t.tick": lambda c, p: None}, [Recurring("t.tick", 60)])
    assert w.run_once()
    rows = conn.execute("SELECT status, run_after FROM jobs WHERE kind = 't.tick' ORDER BY id")
    (s1, _), (s2, after) = rows.fetchall()
    assert (s1, s2) == ("done", "pending")
    assert after > time.time() + 50
