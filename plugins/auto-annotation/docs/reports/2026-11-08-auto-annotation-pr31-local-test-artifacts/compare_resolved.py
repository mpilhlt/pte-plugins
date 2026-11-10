"""Pipeline vs DeepSeek alone on the units the pipeline resolved. Usage: compare_resolved.py <input> <reference> <solo units.csv> <run dir>..."""
import csv, glob, json, sqlite3, sys
from collections import defaultdict
from pte_plugin_auto_annotation.scoring import align_units, f1, ratio, score_unit
from pte_plugin_auto_annotation.units import document_units
PATH = ".//listBibl/bibl"
MODEL = "kisski/deepseek-v4.1-flash-1017"
db = sqlite3.connect("data/db/metadata.db")
def db_units(sid):
    h = db.execute("select id from files where stable_id=?", (sid,)).fetchone()[0]
    return document_units(open(glob.glob(f"data/files/{h[:2]}/{h}*")[0], encoding="utf-8").read(), PATH)
inp, ref = db_units(sys.argv[1]), db_units(sys.argv[2])
al = align_units(inp, ref)
pos = {id(u): i for i, u in enumerate(inp)}
solo = defaultdict(dict)
for r in csv.DictReader(open(sys.argv[3], encoding="utf-8")):
    if r["model"] == MODEL and r["batching"] == "units:8" and r["reference_stable_id"] == sys.argv[2]:
        solo[int(r["repeat"])][int(r["unit_index"])] = (int(r["needed"]), int(r["made"] or 0), int(r["correct_edits"] or 0), r["exact"] == "True")
fmt = lambda v: "-" if v is None else f"{100*v:.1f}"
def line(label, rows):
    n, m, c = (sum(x[i] for x in rows) for i in range(3))
    p, q = ratio(c, m), ratio(c, n)
    print(f"  {label}: units {len(rows)} exact {sum(x[3] for x in rows)} P {fmt(p)} R {fmt(q)} F1 {fmt(f1(p, q))} edits {m}/{n}")
print("solo repeats", {k: len(v) for k, v in solo.items()})
for rep, d in solo.items():
    line(f"solo repeat {rep}, all", list(d.values()))
for run in sys.argv[4:]:
    ann = document_units(open(glob.glob(f"{run}/*.xml")[0], encoding="utf-8").read(), PATH)
    log = json.load(open(glob.glob(f"{run}/*.json")[0], encoding="utf-8"))
    dec = {i: u["decision"] for u in log["units"] for i in u["indices"]}
    pipe = {pos[id(p.input)]: score_unit(p.input.xml, p.gold.xml, ann[pos[id(p.input)]].xml) for p in al.pairs}
    mismatch = sum(1 for i, s in pipe.items() if i in solo[1] and solo[1][i][0] != s.needed)
    res = [i for i in pipe if dec[i] != "unresolved"]
    unres = [i for i in pipe if dec[i] == "unresolved"]
    print("  solo keys in pipeline units:", len(set(solo[1]) & set(pipe)), "of", len(pipe))
    print(run.rstrip("/").split("/")[-1], "needed mismatches vs solo", mismatch, "resolved", len(res), "unresolved", len(unres))
    row = lambda s: (s.needed, s.made, s.correct_edits, s.exact)
    line("pipeline, resolved", [row(pipe[i]) for i in res])
    for rep, d in solo.items():
        line(f"DeepSeek repeat {rep}, same units", [d[i] for i in res if i in d])
        line(f"pipeline resolved + DeepSeek {rep} on unresolved", [row(pipe[i]) for i in res] + [d[i] for i in unres if i in d])
