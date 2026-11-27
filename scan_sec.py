import gzip
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SEC = 'https://www.sec.gov'
TICKERS = ['DVN', 'OXY', 'ORCL', 'C', 'UNH', 'DELL', 'INTC', 'ASTS', 'RDW', 'SMR', 'NNE', 'ONDS']
FORMS = {'8-K', '10-Q', '10-K', '6-K', '20-F'}
DISCOVERY_FORMS = {'8-K', '6-K'}


def fetch(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': os.environ['SEC_USER_AGENT'],
        'Accept-Encoding': 'gzip',
    })
    time.sleep(0.3)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
        return body


def fetch_json(url):
    return json.loads(fetch(url))


def discover(by_cik, today, errors):
    accessions = {}
    index_dates = []
    # EDGAR publishes daily indexes after the filing day closes.
    for offset in range(1, 8):
        date = today - timedelta(days=offset)
        weekday = date.weekday()
        fixed_holidays = {(1, 1), (6, 19), (7, 4), (11, 11), (12, 25)}
        # Saturday holidays are observed Friday; Sunday holidays, Monday.
        observed_date = date + timedelta(days={0: -1, 4: 1}.get(weekday, 0))
        fixed_holiday = (
            (date.month, date.day) in fixed_holidays
            or (observed_date.month, observed_date.day) in fixed_holidays
        )
        floating_holiday = any(
            date.month == month and weekday == day and first <= date.day <= last
            for month, day, first, last in (
                (1, 0, 15, 21), (2, 0, 15, 21), (5, 0, 25, 31),
                (9, 0, 1, 7), (10, 0, 8, 14), (11, 3, 22, 28),
            )
        )
        if weekday >= 5 or fixed_holiday or floating_holiday:
            continue
        quarter = (date.month - 1) // 3 + 1
        url = f'{SEC}/Archives/edgar/daily-index/{date.year}/QTR{quarter}/master.{date:%Y%m%d}.idx'
        try:
            index = fetch(url).decode('latin-1')
            _, separator, rows = index.partition('CIK|Company Name|Form Type|Date Filed|File Name')
            if not separator:
                raise ValueError('EDGAR index header missing')
        except HTTPError as exc:
            if exc.code != 404:
                errors.append(f'{date}: EDGAR index HTTP {exc.code}')
                if exc.code in {403, 429}:
                    break
            continue
        except (OSError, ValueError) as exc:
            errors.append(f'{date}: EDGAR index: {exc}')
            continue
        index_dates.append(date.isoformat())
        for row in rows.splitlines():
            parts = row.split('|')
            if len(parts) != 5:
                continue
            cik, _, form, _, filename = parts
            if form in DISCOVERY_FORMS and cik.isdecimal() and int(cik) in by_cik:
                accessions.setdefault(int(cik), set()).add(Path(filename).stem)
    return accessions, index_dates


def scan(today):
    mapping = fetch_json(f'{SEC}/files/company_tickers_exchange.json')
    by_cik = {}
    watch_tickers = {}
    for row in mapping['data']:
        record = dict(zip(mapping['fields'], row))
        ticker = record['ticker'].upper()
        cik = int(record['cik'])
        if ticker in TICKERS:
            watch_tickers[ticker] = cik
        if record['exchange'] in {'Nasdaq', 'NYSE', 'NYSE American', 'Cboe', 'NYSE Arca'}:
            by_cik.setdefault(cik, []).append(record)

    errors = [f'{ticker}: no SEC CIK mapping' for ticker in TICKERS if ticker not in watch_tickers]
    discovered, index_dates = discover(by_cik, today, errors)
    if not index_dates:
        raise RuntimeError('No EDGAR daily index was available. ' + ' | '.join(errors))

    watch_by_cik = {cik: ticker for ticker, cik in watch_tickers.items()}
    items = []
    for cik in dict.fromkeys([*watch_by_cik, *discovered]):
        try:
            filing = fetch_json(f'https://data.sec.gov/submissions/CIK{cik:010d}.json')
            recent = filing['filings']['recent']
            listing = by_cik.get(cik, [])
            tickers = list(dict.fromkeys(record['ticker'].upper() for record in listing))
            ticker = watch_by_cik.get(cik) or tickers[0]
            indexed = discovered.get(cik, set())
            unresolved = indexed.copy()
            watch_count = 0
            descriptions = recent.get('primaryDocDescription', [''] * len(recent['form']))
            report_dates = recent.get('reportDate', [''] * len(recent['form']))
            for form, date, accession, doc, description, report_date in zip(
                recent['form'], recent['filingDate'], recent['accessionNumber'],
                recent['primaryDocument'], descriptions, report_dates,
            ):
                in_watchlist = cik in watch_by_cik and form in FORMS and watch_count < 5
                if in_watchlist:
                    watch_count += 1
                if not (in_watchlist or accession in indexed):
                    continue
                if not doc:
                    errors.append(f'{accession}: primary document missing')
                    continue
                unresolved.discard(accession)
                items.append({
                    'ticker': ticker, 'tickers': tickers, 'company': filing['name'],
                    'form': form, 'filed': date, 'report_date': report_date, 'accession': accession,
                    'url': f'{SEC}/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{quote(doc)}',
                    'description': description,
                    'discovery': cik not in watch_by_cik,
                })
            errors.extend(f'{accession}: filing missing from issuer submissions' for accession in sorted(unresolved))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f'CIK {cik}: {exc}')
    items.sort(key=lambda item: (item['filed'], item['accession']), reverse=True)
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'SEC EDGAR daily indexes and public submissions API',
        'scope': 'Recent 8-K and 6-K filings from SEC-mapped exchange listings, plus watchlist history',
        'index_dates': sorted(index_dates, reverse=True),
        'filings': items,
        'errors': errors,
    }


def main():
    ua = os.environ.get('SEC_USER_AGENT', '')
    if '@' not in ua:
        raise SystemExit('Set SEC_USER_AGENT to a descriptive name and real contact email in GitHub Actions secrets.')
    output = scan(datetime.now(ZoneInfo('America/New_York')).date())
    path = ROOT / 'data' / 'sec-catalysts.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2) + '\n')
    discoveries = sum(item['discovery'] for item in output['filings'])
    print(f"Wrote {len(output['filings'])} filings ({discoveries} outside the watchlist); {len(output['errors'])} errors")


if __name__ == '__main__':
    main()
