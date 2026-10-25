import json
import os

from afr.cli import main


def _first_dir(root: str) -> str:
    return sorted(os.listdir(root))[-1]


# -- record ----------------------------------------------------------------

def test_record_creates_complete_session(tmp_path):
    rc = main(["record", str(tmp_path / "ws"), "--scenario", "planner",
               "--store", str(tmp_path / "store")])
    assert rc == 0
    store_root = str(tmp_path / "store")
    sid = _first_dir(store_root)
    assert os.path.exists(os.path.join(store_root, sid, "trace.jsonl"))
    assert os.path.exists(os.path.join(store_root, sid, "manifest.json"))


def test_record_failing_scenario_exits_nonzero(tmp_path):
    rc = main(["record", str(tmp_path / "ws"), "--scenario", "failing",
               "--store", str(tmp_path / "store")])
    assert rc == 1   # tool error inside the recorded run -- CLI reflects it


# -- replay / verify ---------------------------------------------------------

def test_replay_recorded_mode(tmp_path):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "planner", "--store", store])
    rc = main(["replay", _first_dir(store), "--store", store])
    assert rc == 0


def test_replay_verify_detects_drift(tmp_path, capsys):
    store = str(tmp_path / "store")
    ws = str(tmp_path / "ws")
    main(["record", ws, "--scenario", "planner", "--store", store])
    with open(os.path.join(ws, "notes.txt"), "a", encoding="utf-8") as f:
        f.write("changed later\n")
    rc = main(["replay", _first_dir(store), "--verify", "--store", store, "--workspace", ws])
    assert rc == 1
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert report["deviations"] >= 1


def test_verify_report_tamper(tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "auditor", "--store", store])
    sid = _first_dir(store)
    assert main(["verify", sid, "--store", store]) == 0
    path = os.path.join(store, sid, "trace.jsonl")
    with open(path, "a", encoding="utf-8") as f:          # roll-on tamper: add to manifest hash source
        f.write('{"junk": 1}\n')
    # ...which breaks chain because content hash != manifest's recorded one
    assert main(["verify", sid, "--store", store]) == 1


def test_list(tmp_path, capsys):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "auditor", "--store", store])
    assert main(["list", "--store", store]) == 0
    out = capsys.readouterr().out
    assert "complete" in out


# -- prefetch ----------------------------------------------------------------

def test_prefetch_ledger_written(tmp_path):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "planner", "--store", store])
    out = str(tmp_path / "ledger.json")
    rc = main(["prefetch", _first_dir(store), "--store", store,
               "--workspace", str(tmp_path / "ws"), "--out", out,
               "--train", str(tmp_path / "model.json")])
    assert rc == 0
    ledger = json.load(open(out, encoding="utf-8"))
    assert "hit_rate" in ledger and ledger["mode"] == "offline"


def test_prefetch_refuses_missing_session(tmp_path, capsys):
    rc = main(["prefetch", "ghost", "--store", str(tmp_path / "store"),
               "--workspace", str(tmp_path / "ws")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no session matches" in err


# -- diff ---------------------------------------------------------------------

def test_diff_pairs(tmp_path, capsys):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "planner", "--store", store])
    main(["record", str(tmp_path / "ws"), "--scenario", "auditor", "--store", store])
    dirs = sorted(os.listdir(store))
    rc = main(["diff", dirs[0], dirs[1], "--store", store, "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out[out.index("{"):])
    assert "similarity" in parsed


# -- view ---------------------------------------------------------------------

def test_view_writes_report(tmp_path):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "planner", "--store", store])
    out = str(tmp_path / "report.html")
    rc = main(["view", _first_dir(store), "--store", store, "-o", out])
    assert rc == 0 and os.path.exists(out)


def test_view_writes_markdown_and_prints_terminal(tmp_path, capsys):
    store = str(tmp_path / "store")
    ws = str(tmp_path / "ws")
    main(["record", ws, "--scenario", "planner", "--store", store])
    sid = _first_dir(store)
    out_md = str(tmp_path / "out" / "report.md")
    rc = main(["view", sid, "--store", store, "-o", out_md])
    assert rc == 0
    term = capsys.readouterr().out
    assert "AGENT FLIGHT REPORT" in term            # terminal form on stdout
    body = open(out_md, encoding="utf-8").read()    # markdown file on disk
    assert "## At a glance" in body
    assert sid in body


def test_view_writes_html_alongside_markdown(tmp_path):
    store = str(tmp_path / "store")
    main(["record", str(tmp_path / "ws"), "--scenario", "planner", "--store", store])
    out_md = str(tmp_path / "report.md")
    rc = main(["view", _first_dir(store), "--store", store, "-o", out_md, "--html"])
    assert rc == 0
    assert os.path.exists(out_md)
    assert os.path.exists(str(tmp_path / "report.html"))
