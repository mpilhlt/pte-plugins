"""Score an annotated version against a reference with the plugin's own scoring. Usage: compare.py <input> <annotated> <reference> <run-log.json>"""
import glob, json, sqlite3, sys
from collections import Counter, defaultdict
from pte_plugin_auto_annotation.scoring import align_units, score_unit, ratio, f1
from pte_plugin_auto_annotation.units import document_units

PATH = ".//listBibl/bibl"
db = sqlite3.connect("data/db/metadata.db")
def units(stable_id):
    h = db.execute("select id from files where stable_id=?", (stable_id,)).fetchone()[0]
    return document_units(open(glob.glob(f"data/files/{h[:2]}/{h}*")[0], encoding="utf-8").read(), PATH)

inp, ann, ref = (units(s) for s in sys.argv[1:4])
log = json.load(open(sys.argv[4], encoding="utf-8"))
al = align_units(inp, ref)
print(f"input units {len(inp)}, annotated {len(ann)}, reference {len(ref)}; aligned by text {len(al.pairs)}; unmatched input {al.unmatched_input}, unmatched reference {al.unmatched_gold}")
pos = {id(u): i for i, u in enumerate(inp)}
decisions = {}
for u in log["units"]:
    for i in u["indices"]:
        decisions[i] = u

def table(label, proposal):
    s = [score_unit(p.input.xml, p.gold.xml, proposal(pos[id(p.input)], p)) for p in al.pairs]
    needed, made, correct = (sum(getattr(x, a) for x in s) for a in ("needed", "made", "correct_edits"))
    pr, re = ratio(correct, made), ratio(correct, needed)
    gf, pf, cf = (sum(getattr(x, a) for x in s) for a in ("gold_fields", "proposal_fields", "correct_fields"))
    fp, fr = ratio(cf, pf), ratio(cf, gf)
    fmt = lambda v: "-" if v is None else f"{100*v:.1f}"
    print(f"| {label} | {len(s)} | {sum(x.exact for x in s)} ({100*sum(x.exact for x in s)/len(s):.1f}%) | {fmt(pr)} | {fmt(re)} | {fmt(f1(pr, re))} | {fmt(f1(fp, fr))} | {made}/{needed} |")
    return s

print("| Version | Units | Exact | Edit P | Edit R | Edit F1 | Field F1 | Edits made/needed |\n| --- | --- | --- | --- | --- | --- | --- | --- |")
table("extraction (baseline)", lambda i, p: p.input.xml)
result = table("auto-annotation", lambda i, p: ann[i].xml)
print("\nBy decision:")
groups = defaultdict(list)
for p, s in zip(al.pairs, result):
    d = decisions[pos[id(p.input)]]
    groups[d["decision"] + (":" + str(d.get("chosen")) if d["decision"] == "arbitrated" else "")].append(s)
print("| Decision | Units | Exact | Edit P | Edit R |\n| --- | --- | --- | --- | --- |")
for k, v in sorted(groups.items()):
    n, m, c = sum(x.needed for x in v), sum(x.made for x in v), sum(x.correct_edits for x in v)
    fmt = lambda x: "-" if x is None else f"{100*x:.1f}"
    print(f"| {k} | {len(v)} | {sum(x.exact for x in v)} | {fmt(ratio(c,m))} | {fmt(ratio(c,n))} |")
print("\nall decisions:", Counter(u["decision"] for u in log["units"]), Counter(u.get("chosen") for u in log["units"] if u["decision"] == "arbitrated"))
print("statuses A:", Counter(u["statuses"].get("A") for u in log["units"]), "B:", Counter(u["statuses"].get("B") for u in log["units"]), "arbiter:", Counter(u.get("arbiter_status") for u in log["units"]))
print("groups spanning >1 unit:", sum(len(u["indices"]) > 1 for u in log["units"]))
r = log["runs"]
print("runs keys:", {k: (list(v) if isinstance(v, dict) else type(v).__name__) for k, v in r.items()})
for k, v in r.items():
    if isinstance(v, dict):
        print(k, {a: b for a, b in v.items() if not isinstance(b, (list, dict))})
        for a, b in v.items():
            if isinstance(b, list) and b: print(" ", a, len(b), str(b[0])[:280])
