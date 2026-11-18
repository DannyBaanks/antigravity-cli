import json, sys, time
raw = sys.stdin.read()
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"t": time.time(), "stdin": json.loads(raw) if raw.strip() else None}) + "\n")
print(json.dumps({"injectSteps": [{"errorMessage": {"userMessage": "HOOKTEST: turno cortado antes del modelo"}}], "terminationBehavior": "terminate"}))
