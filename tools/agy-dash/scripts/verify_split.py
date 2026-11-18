import ast, sys, subprocess
from pathlib import Path
orig_src = subprocess.run(["git","-C",sys.argv[1],"show","f255869:tools/agy-dash/agy_dashboard.py"],capture_output=True,text=True,check=True).stdout
pkg = Path(sys.argv[1])/"tools/agy-dash/agy_dash"
MODS=["paths","procs","sessions","tmuxctl","accounts","compact","mirror","tui","cli"]
def defs(tree):
    out={}
    for n in tree.body:
        if isinstance(n,(ast.FunctionDef,ast.ClassDef)): out[n.name]=n
        elif isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name): out[n.targets[0].id]=n
        elif isinstance(n,ast.AnnAssign): out[n.target.id]=n
    return out
class Norm(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value,ast.Name) and node.value.id in {f"_{m}" for m in MODS}:
            if node.value.id=="_paths" and node.attr=="SELF_PATH":
                return ast.parse("Path(__file__).resolve()",mode="eval").body
            return ast.Name(id=node.attr, ctx=node.ctx)
        return node
def dump(n): return ast.dump(n, include_attributes=False)
o=defs(ast.parse(orig_src)); new={}
for m in MODS:
    for k,v in defs(ast.parse((pkg/f"{m}.py").read_text())).items():
        assert k not in new, k; new[k]=Norm().visit(v)
new.pop("SELF_PATH")
# SCRIPT_DIR intentionally anchored one level up
o.pop("SCRIPT_DIR"); new.pop("SCRIPT_DIR")
missing=set(o)-set(new); extra=set(new)-set(o)
diff=[k for k in o if k in new and dump(o[k])!=dump(new[k])]
print(f"orig={len(o)} new={len(new)} missing={missing} extra={extra} differing={diff}")
sys.exit(1 if (missing or extra or diff) else 0)
