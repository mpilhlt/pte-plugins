"""Score a dry-run pipeline result. Usage: score_dry.py <input stable id> <reference stable id> <run dir>..."""
import glob
import json
import sqlite3
import sys
from collections import Counter, defaultdict

from pte_plugin_auto_annotation.scoring import align_units, f1, ratio, score_unit
from pte_plugin_auto_annotation.units import document_units

PATH = ".//listBibl/bibl"
db = sqlite3.connect("data/db/metadata.db")


def db_units(stable_id):
    h = db.execute("select id from files where stable_id=?", (stable_id,)).fetchone()[0]
    return document_units(open(glob.glob(f"data/files/{h[:2]}/{h}*")[0], encoding="utf-8").read(), PATH)


inp, ref = db_units(sys.argv[1]), db_units(sys.argv[2])
al = align_units(inp, ref)
pos = {id(u): i for i, u in enumerate(inp)}
print(f"input {len(inp)}, reference {len(ref)}, aligned {len(al.pairs)}")
fmt = lambda v: "-" if v is None else f"{100 * v:.1f}"


def line(label, scores):
    n, m, c = (sum(getattr(x, a) for x in scores) for a in ("needed", "made", "correct_edits"))
    p, r = ratio(c, m), ratio(c, n)
    gf, pf, cf = (sum(getattr(x, a) for x in scores) for a in ("gold_fields", "proposal_fields", "correct_fields"))
    fp, fr = ratio(cf, pf), ratio(cf, gf)
    print(f"  {label}: units {len(scores)} exact {sum(x.exact for x in scores)} P {fmt(p)} R {fmt(r)} F1 {fmt(f1(p, r))} fieldF1 {fmt(f1(fp, fr))} edits {m}/{n}")


for run in sys.argv[3:]:
    xml = glob.glob(f"{run}/*.xml")[0]
    log = json.load(open(glob.glob(f"{run}/*.json")[0], encoding="utf-8"))
    ann = document_units(open(xml, encoding="utf-8").read(), PATH)
    assert len(ann) == len(inp), (len(ann), len(inp))
    decisions = {i: u for u in log["units"] for i in u["indices"]}
    scores = [score_unit(p.input.xml, p.gold.xml, ann[pos[id(p.input)]].xml) for p in al.pairs]
    print(run.rstrip("/").split("/")[-1], log["models"], log["counts"])
    line("all", scores)
    groups = defaultdict(list)
    for p, s in zip(al.pairs, scores):
        d = decisions[pos[id(p.input)]]
        groups[d["decision"] + (":" + str(d.get("chosen")) if d["decision"] == "arbitrated" else "")].append(s)
    for k in sorted(groups):
        line(k, groups[k])
    chosen = Counter(u.get("chosen") for u in log["units"] if u["decision"] == "arbitrated")
    both = Counter(u.get("chosen") for u in log["units"] if u["decision"] == "arbitrated" and u["statuses"].get("A") in ("ok", "unchanged") and u["statuses"].get("B") in ("ok", "unchanged"))
    invalid = {k: sum(1 for u in log["units"] if u["statuses"].get(k) not in ("ok", "unchanged", "error", None)) for k in ("A", "B")}
    print("  chosen", dict(chosen), "both valid", dict(both), "invalid", invalid)
    for k, r in log["runs"].items():
        print(f"  run {k}: calls {r['calls']} retries {r['retries']} seconds {r['seconds']:.1f} prompt chars {r['prompt_chars']}")
