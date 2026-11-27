import json, os, re, time, urllib.request
from pathlib import Path
from html import unescape
from urllib.parse import urljoin, urlsplit
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

def fetch_html(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,text/plain",
        "Accept-Encoding": "identity"
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read(2000000)
    return raw.decode("utf-8", errors="replace")

def plain_text(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text))

def earnings_release_url(html, filing_url):
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.I | re.S):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)
        if len(cells) < 2 or plain_text(cells[0]).strip().rstrip("*").rstrip() != "99.1":
            continue
        for anchor in re.finditer(r"""<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>(.*?)</a>""", row, re.I | re.S):
            if not re.search(r"press release|earnings release", plain_text(anchor[3]), re.I):
                continue
            url = urljoin(filing_url, unescape(anchor[2]).strip())
            target = urlsplit(url)
            if (target.scheme == "https" and target.netloc.lower() in {"www.sec.gov", "sec.gov"}
                    and target.path.lower().startswith("/archives/edgar/data/")
                    and target.path.lower().endswith((".html", ".htm"))):
                return url
    return None

def earnings_readout(html):
    # Keep prose blocks separate so table cells do not turn into headlines.
    blocks = re.split(r"</(?:p|div|li|h[1-6])\s*>|<br\b[^>]*>", html, flags=re.I)
    sentences = []
    for block in blocks:
        prose = " ".join(plain_text(block).split())
        sentences.extend(re.split(r"(?<![A-Z]\.)(?<![A-Z])(?<=[.!?])\s+(?=[A-Z“])", prose))

    sentences = [line.strip(" “”).") + "." for line in sentences if 35 <= len(line) <= 340]
    reported = []
    outlook = []
    for line in sentences:
        if re.search(r"\b(?:expects?|anticipates?|outlook|guidance|forecasts?|targets?|projects?)\b", line, re.I):
            if re.search(r"\b(?:quarter|full.year|fiscal|20\d{2}|Q[1-4])\b", line, re.I):
                outlook.append(line)
        elif (re.search(r"\b(?:reported|generated|was|were|increased|decreased|grew|rose|fell)\b", line, re.I)
              and re.search(r"\b(?:revenue|net sales|net income|earnings per share)\b", line, re.I)
              and re.search(r"\$\s*\d", line)):
            reported.append(line)

    result = max(reported, key=lambda line: bool(re.search(r"\b(?:compared|versus|prior)\b", line, re.I)), default="")
    guidance = max(
        outlook,
        key=lambda line: bool(re.search(r"\b(?:to at least|more than|range of|between|expects?)\b.*(?:\$\s*\d|\d%)", line, re.I)),
        default=""
    )
    if not (result or guidance):
        return {}

    if re.search(r"\bacquisitions?\b", plain_text(html), re.I):
        question = "How much of the reported growth came from acquisitions, and how much came from the existing business?"
    elif result:
        question = "How did operating cash flow compare with the reported result?"
    else:
        question = "How does this outlook compare with management’s previous guidance?"

    return {"reported": result, "outlook": guidance, "question": question}

def share_issuance_readout(text):
    # Require an explicit quantity and issuance wording in an Other Events item.
    item = re.search(r"\bitem\s+8\.01\b(.*?)(?=\bitem\s+9\.01\b|$)", text, re.I)
    if not item:
        return []
    pattern = re.compile(
        r"(?P<count>\b\d[\d,]*) shares of (?:the )?company[’']s "
        r"(?:class [a-z] )?common stock (?:\([^)]*\) )?that the company "
        r"(?P<action>may elect to issue|may issue|issued) to "
        r"(?P<recipient>[^.;(]{2,100})", re.I
    )
    return [
        {
            "status": "Issued" if match["action"].lower() == "issued" else "Potential issuance",
            "shares": int(match["count"].replace(",", "")),
            "excerpt": match[0].strip(),
        }
        for match in pattern.finditer(item[1])
    ]


def private_financing_readout(text):
    agreement = re.search(
        r"\bitem\s+1\.01\b(.*?)(?=\bitem\s+\d\.\d{2}\b|$)", text, re.I
    )
    if not agreement:
        return None
    # Keep the signed agreement, proceeds, and share terms in the same sentence.
    match = re.search(
        r"(?P<evidence>\bentered into [^!?]{1,700}?"
        r"\b(?:private placement|private investment in public equity)\b"
        r"[^!?]{0,400}?\b(?:aggregate purchase price|gross proceeds) of "
        r"(?:approximately )?\$[\d,.]+(?:\s+(?:million|billion))?)",
        agreement[1], re.I
    )
    if match:
        return {"item": "1.01", "excerpt": match["evidence"].rstrip(".")}
    return None


OFFERING_DISCLAIMER = re.compile(
    r"\b(?:does not constitute|is not|shall not (?:be|constitute)|"
    r"not to be construed as|is not intended to constitute)\b"
    r"[^.!?;]{0,220}\bpublic offering\b", re.I
)


def financing_share_details(filing_text, release_text=""):
    number = r"\d[\d,]*(?:\.\d+)?(?:\s+(?:million|billion))?"
    combined = re.search(
        rf"\bissued an aggregate of (?P<count>{number}) shares\b"
        r"[^.!?;]{0,160}\bstockholders and the investors in the concurrent financing\b",
        filing_text, re.I,
    )
    ownership = re.search(
        r"\bon a fully diluted basis\b(?:[^.!?;]|\.(?=\d)){0,700}?"
        r"\b(?:the investors in the concurrent financing|private placement investors)"
        r"\s+owned(?:,\s+in the aggregate,)?\s+(?:approximately\s+)?"
        r"(?P<percent>\d+(?:\.\d+)?)%",
        filing_text, re.I,
    )
    outstanding = re.search(
        rf"\bthe combined company had (?:approximately\s+)?(?P<count>{number})"
        r"\s+shares of common stock issued and outstanding\b",
        release_text, re.I,
    )

    details = []
    if outstanding:
        details.append(
            f"Common shares outstanding after closing: {outstanding['count']}."
        )
    if ownership:
        details.append(
            f"Placement investors held about {ownership['percent']}% "
            "on a fully diluted basis."
        )
    if combined:
        details.append(
            f"The 8-K reports {combined['count']} shares issued to existing "
            "shareholders and placement investors combined. That figure does "
            "not isolate the placement share count."
        )
    return " ".join(details)


def financing_event_readout(prose):
    """Summarize financing events supported by the filing text."""
    equity = re.search(
        r"\b(?:private placement|private investment in public equity)\b"
        r"[^;!?]{0,160}?\b(?:common stock|ordinary shares)\b"
        r"[^;!?]{0,160}?\b(?:aggregate purchase price|gross proceeds)\s+of\s+"
        r"(?P<amount>(?:approximately\s+)?\$[\d,]+(?:\.\d+)?"
        r"(?:\s+(?:million|billion))?)",
        prose, re.I,
    )
    if equity:
        return {
            "label": "Equity financing",
            "priority": "High",
            "what_changed": (
                f"The filing describes a private equity placement of {equity['amount']}."
            ),
            "why_it_matters": (
                "The financing can add cash and increase the share count."
            ),
            "what_to_check": (
                "Verify the price per share, number of shares, closing status, "
                "and post-deal share count."
            ),
            "evidence": equity[0],
        }

    debt = re.search(
        r"\b(?:completed|closed|priced|announced)\s+"
        r"(?:the|a|an|its)\s+(?:registered\s+)?(?:public\s+)?offering\b"
        r"[^;!?]{0,220}?\b(?:senior(?:\s+(?:unsecured|secured|subordinated))?"
        r"\s+notes|unsecured notes|secured notes)\b"
        r"(?:\s+due\s+20\d{2})?",
        prose, re.I,
    )
    if debt:
        return {
            "label": "Debt financing",
            "priority": "Medium",
            "what_changed": (
                "The filing describes a debt offering involving notes."
            ),
            "why_it_matters": (
                "The terms can affect interest expense and repayment timing."
            ),
            "what_to_check": (
                "Verify the full offering size, interest rates, maturities, "
                "closing status, and use of proceeds."
            ),
            "evidence": debt[0],
        }

    extension = re.search(
        r"\bsponsor of (?:the )?initial public offering\b"
        r"[^;.!?]{0,230}?\bdeposited (?:an? )?extension payment of\s+"
        r"(?P<amount>\$[\d,]+(?:\.\d+)?)\s+into the trust account\b",
        prose, re.I,
    )
    if extension:
        return {
            "label": "SPAC deadline extension",
            "priority": "Low",
            "what_changed": (
                f"The SPAC sponsor deposited {extension['amount']} "
                "into the trust account as an extension payment."
            ),
            "why_it_matters": (
                "The payment buys more time for the SPAC to complete a deal."
            ),
            "what_to_check": (
                "Verify the revised deadline and any scheduled shareholder vote."
            ),
            "evidence": extension[0],
        }

    return None


def analyze(filing):
    result = dict(filing)
    result["status"] = "Analyzed"
    result["signals"] = []
    result["score"] = 0
    try:
        html = fetch_html(filing["url"])
        prose = " ".join(plain_text(html).split())
        text = prose.lower()
        if filing.get("form") in {"8-K", "8-K/A", "6-K", "6-K/A"}:
            event = financing_event_readout(prose)
            if event:
                if event["label"] == "Equity financing":
                    release_text = ""
                    release_url = earnings_release_url(html, filing["url"])
                    if release_url:
                        try:
                            time.sleep(0.3)
                            release_text = " ".join(
                                plain_text(fetch_html(release_url)).split()
                            )
                        except (OSError, ValueError):
                            pass
                    share_details = financing_share_details(prose, release_text)
                    if share_details:
                        event["what_changed"] += " " + share_details
                result["financing_event"] = event
        if filing.get("form") in {"8-K", "8-K/A"}:
            financing = private_financing_readout(prose)
            if financing:
                result["private_financing"] = financing
            issuances = share_issuance_readout(prose)
            if issuances:
                result["share_issuances"] = issuances
        if filing.get("form") in {"8-K", "8-K/A"} and re.search(r"\bitem\s+2\.02\b", text):
            result["earnings_results"] = True
            release_url = earnings_release_url(html, filing["url"])
            if release_url:
                result["earnings_release_url"] = release_url
                try:
                    time.sleep(0.3)
                    readout = earnings_readout(fetch_html(release_url))
                    if readout:
                        result["earnings_readout"] = readout
                except (OSError, ValueError):
                    # Preserve the filing and exhibit link when the exhibit is unavailable.
                    pass
        # Blank only the matched words, preserving offsets and other evidence.
        disclaimer_text = OFFERING_DISCLAIMER.sub(
            lambda match: match[0][:-len("public offering")] + " " * len("public offering"),
            text
        )
        for category, phrases in RULES.items():
            for phrase, weight in phrases.items():
            
                pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
                searchable = disclaimer_text if phrase == "public offering" else text
                found = list(re.finditer(pattern, searchable))
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
