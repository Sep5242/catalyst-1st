import json, os, re, time, urllib.request
from pathlib import Path
from html import unescape
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "sec-catalysts.json"
OUTPUT = ROOT / "data" / "sec-analysis.json"
UA = os.environ.get("SEC_USER_AGENT", "")
if not UA or "@" not in UA:
    raise SystemExit("SEC_USER_AGENT must contain a contact email")

RULES = {
    "Positive": {
        "new contract": 3, "contract award": 3,
        "increased guidance": 4, "raises guidance": 4,
        "strategic partnership": 2, "share repurchase": 2,
        "revenue increased": 2
    },
    "Negative": {
        "going concern": 5, "material weakness": 4,
        "public offering": 3, "at-the-market offering": 3,
        "substantial doubt": 5, "decreased guidance": 4,
        "lowers guidance": 4
    }
}

def fetch_text(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,text/plain",
        "Accept-Encoding": "identity"
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read(2000000)
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text))

def analyze(filing):
    result = dict(filing)
    result["status"] = "Analyzed"
    result["signals"] = []
    result["score"] = 0
    try:
        text = fetch_text(filing["url"]).lower()
        for category, phrases in RULES.items():
            for phrase, weight in phrases.items():
            
                pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
                found = list(re.finditer(pattern, text))
                matches = len(found)
                excerpts = [
                    text[max(0, m.start()-120):min(len(text), m.end()+120)]
                    for m in found[:3]
                ]
                if matches:
    
                    points = weight if category == "Positive" else -weight
                    result["score"] += points
                    result["signals"].append({
                        "category": category,
                        "phrase": phrase,
                        "mentions": matches,
                        "points": points,
                         "excerpts":excerpts
                    })
        result["classification"] = (
            "Potential positive" if result["score"] > 0
            else "Potential negative" if result["score"] < 0
            else "No rule-based signal"
        )
    except Exception as exc:
        result["status"] = "Fetch error"
        result["classification"] = "Not analyzed"
        result["error"] = str(exc)[:250]
    return result

def main():
    source = json.loads(SOURCE.read_text())
    filings = source.get("filings", [])
    results = []
    for filing in filings:
        results.append(analyze(filing))
        time.sleep(0.3)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Rule-based phrase screening; not AI interpretation",
        "disclaimer": "Signals require manual verification in the SEC filing.",
        "filings": results
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n")
    errors = sum(x["status"] != "Analyzed" for x in results)
    print(f"Analyzed {len(results)} filings; {errors} fetch errors")

if __name__ == "__main__":
    main()