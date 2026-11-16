import json, os, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
(ROOT / 'data').mkdir(parents=True, exist_ok=True)
TICKERS = ['DVN','OXY','ORCL','C','UNH','DELL','INTC','ASTS','RDW','SMR','NNE','ONDS']
FORMS = {'8-K','10-Q','10-K','6-K','20-F'}
UA = os.environ.get('SEC_USER_AGENT','')
if not UA or '@' not in UA:
    raise SystemExit('Set SEC_USER_AGENT to a descriptive name and real contact email in GitHub Actions secrets.')
HEADERS={'User-Agent': UA, 'Accept-Encoding':'gzip, deflate', 'Accept':'application/json'}
def fetch(url):
    req=urllib.request.Request(url,headers=HEADERS)
    with urllib.request.urlopen(req,timeout=30) as resp:
        body=resp.read()
        if resp.headers.get('Content-Encoding')=='gzip':
            import gzip
            body=gzip.decompress(body)
        return json.loads(body)

mapping=fetch('https://www.sec.gov/files/company_tickers.json')
by_ticker={v['ticker'].upper():v for v in mapping.values()}
items=[]; errors=[]
for ticker in TICKERS:
    record=by_ticker.get(ticker)
    if not record:
        errors.append(f'{ticker}: no SEC CIK mapping');continue
    cik=int(record['cik_str'])
    try:
        filing=fetch(f'https://data.sec.gov/submissions/CIK{cik:010d}.json')
        recent=filing['filings']['recent']
        for i,(form,date) in enumerate(zip(recent['form'],recent['filingDate'])):
            if form not in FORMS:continue
            accession=recent['accessionNumber'][i]
            doc=recent['primaryDocument'][i]
            url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{doc}'
            items.append({'ticker':ticker,'company':record['title'],'form':form,'filed':date,'accession':accession,'url':url,'description':recent.get('primaryDocDescription',['']*len(recent['form']))[i]})
            if sum(x['ticker']==ticker for x in items)>=5:break
    except Exception as exc:
        errors.append(f'{ticker}: {type(exc).__name__}: {exc}')
    time.sleep(.3)
items.sort(key=lambda x:x['filed'],reverse=True)
out={'generated_at':datetime.now(timezone.utc).isoformat(),'source':'SEC EDGAR public submissions API','scope':'Selected watchlist tickers; recent filings only, not all-market discovery','filings':items,'errors':errors}
(ROOT/'data'/'sec-catalysts.json').write_text(json.dumps(out,indent=2)+'\n')
print(f'Wrote {len(items)} filings; {len(errors)} errors')
