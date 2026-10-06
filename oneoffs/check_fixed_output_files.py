import json

with open("/tmp/found-literals-fixed.json") as fh:
    data = json.load(fh)

files = sorted({item["file"] for item in data})
print(f"{len(data)} findings across {len(files)} files:")
for f in files:
    print(f"  {f}")
