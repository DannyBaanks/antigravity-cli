import json
import sys

raw = sys.stdin.read()
d = json.loads(raw) if raw.strip() else {}
with open(sys.argv[1], "a") as f:
    keys = ("invocationNum", "initialNumSteps", "lastUserInput", "conversationId")
    f.write(json.dumps({k: d.get(k) for k in keys}, ensure_ascii=False) + "\n")
print("{}")
