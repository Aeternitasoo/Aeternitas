"""Offline user-supplied Yahoo export import. No provider access or time invention."""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from market_data import CSV_FIELDS, DailyBar, SessionCalendar, iso_day, load_csv, number
from time_machine import run, save_manifest

VERSION = "yahoo-user-csv-v1"
DEFAULT_ROOT = (Path.home() / '.local/share/aeternitas-lab/datasets/vst')
FIELDS = ('date', 'open', 'high', 'low', 'close', 'adj_close', 'volume')


def _column(value):
    name = value.strip().lower().replace('_', ' ')
    return {'date': 'date', 'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close',
            'adj close': 'adj_close', 'adjusted close': 'adj_close', 'volume': 'volume'}.get(name)


def _write(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def import_csv(path, *, root=DEFAULT_ROOT, calendar_path=None, cutoffs=(), expected_sha256=None, synthetic=False):
    """Always archive bounded input bytes; invalid data produces no normalized CSV.

    Source acquisition/publication are unknown. The local import time is recorded
    separately and never substituted for either timestamp in DailyBar.
    """
    cutoffs = tuple(cutoffs)
    from point_in_time import parse_timestamp
    for cutoff in cutoffs: parse_timestamp(cutoff)
    with Path(path).open('rb') as stream: raw = stream.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024: raise ValueError('source_size_limit')
    sha = hashlib.sha256(raw).hexdigest()
    stamp = datetime.now(timezone.utc).isoformat()
    folder = Path(root) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('raw', 'normalized', 'validation'): (folder/name).mkdir()
    with (folder/'raw/input.csv').open('xb') as stream: stream.write(raw)
    errors, transformations, parsed = [], [], []
    warnings = ['historical_availability_unknown', 'source_download_time_unknown', 'adjustment_semantics_unverified']
    calendar = None
    if expected_sha256 is not None and expected_sha256 != sha:
        errors.append({'code': 'source_hash_mismatch'})
    try:
        if calendar_path:
            # Copy once, then parse the copied bytes; no mutable path dependency.
            with Path(calendar_path).open('rb') as stream: calraw = stream.read(5 * 1024 * 1024 + 1)
            if len(calraw) > 5 * 1024 * 1024: raise ValueError('calendar_size_limit')
            with (folder/'raw/calendar.json').open('xb') as stream: stream.write(calraw)
            calendar = SessionCalendar.load(folder/'raw/calendar.json')
            if (calendar.exchange, calendar.timezone) != ('XNYS', 'America/New_York'):
                raise ValueError('calendar_identity_mismatch')
        reader = csv.reader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
        header = next(reader)
        columns = [_column(c) for c in header]
        if None in columns or set(columns) != set(FIELDS) or len(columns) != len(FIELDS):
            raise ValueError('columns_missing_unknown_or_ambiguous')
        if header != list(FIELDS): transformations.append({'rule': 'explicit_header_aliases', 'mapping': dict(zip(header, columns))})
        seen = set()
        for line, values in enumerate(reader, 2):
            if line > 20001: raise ValueError('row_limit')
            try:
                if len(values) != len(columns): raise ValueError('row_width')
                row = dict(zip(columns, values))
                day = iso_day(row['date'].strip())
                if day in seen: raise ValueError('duplicate_date')
                seen.add(day)
                prices = {k: number(row[k].strip()) for k in ('open','high','low','close','adj_close')}
                if prices['adj_close'] <= 0: raise ValueError('invalid_adjusted_close')
                volume = number(row['volume'].strip())
                if volume != volume.to_integral_value(): raise ValueError('fractional_volume')
                DailyBar('VST','XNYS','America/New_York',day,prices['open'],prices['high'],prices['low'],prices['close'],
                         int(volume),'USD','user_supplied_yahoo_export',sha,sha,None,'unknown',None,'unknown','user_supplied')
                if any(v != v.strip() for v in values): transformations.append({'rule':'trim_cell_whitespace','line':line})
                parsed.append({'line':line, 'date':day, **prices, 'volume':int(volume)})
            except ValueError as error:
                errors.append({'line':line, 'code':str(error)})
        if not parsed: errors.append({'code':'no_valid_rows'})
    except (ValueError, UnicodeError, csv.Error, StopIteration, OSError) as error:
        errors.append({'code':str(error) or type(error).__name__})
    dates = [r['date'] for r in parsed]
    if dates != sorted(dates):
        transformations.append({'rule':'sort_ascending_date','affected_source_lines':[r['line'] for r in parsed]})
    parsed.sort(key=lambda r:r['date'])
    missing = []
    if parsed and calendar:
        if calendar.coverage_start > min(dates) or calendar.coverage_end < max(dates):
            errors.append({'code':'calendar_coverage_missing'})
        missing = sorted(str(d) for d in calendar.sessions if min(dates)<=d<=max(dates) and d not in set(dates))
        unexpected = sorted(str(d) for d in dates if d not in calendar.sessions)
        if unexpected: errors.append({'code':'non_session_dates','dates':unexpected})
        if missing: warnings.append('missing_trading_sessions')
    else: warnings.append('trading_session_coverage_unverified')
    if parsed and any(r['volume']==0 for r in parsed): warnings.append('zero_volume_observed')
    anomalies = []
    for previous, current in zip(parsed, parsed[1:]):
        if abs(current['close'] / previous['close'] - 1) > number('0.5'):
            anomalies.append({'line':current['line'],'code':'close_change_over_50_percent_review_only'})
    if anomalies: warnings.append('large_price_changes_require_review')
    quality = {'status':'failed' if errors else 'passed_with_warnings','errors':errors,'warnings':warnings,
               'parsed_record_count':len(parsed), 'missing_sessions':missing,'anomalies':anomalies,'transformations':transformations,
               'outlier_policy':'Nonfinite, nonpositive and invalid OHLC rejected; no statistical price repair or corporate-action inference.'}
    if not errors:
        with (folder/'normalized/market.csv').open('x',newline='') as stream:
            writer = csv.DictWriter(stream,sorted(CSV_FIELDS)); writer.writeheader()
            for r in parsed:
                writer.writerow(dict(symbol='VST',exchange='XNYS',timezone='America/New_York',trade_date=str(r['date']),
                    **{k:str(r[k]) for k in ('open','high','low','close','volume')},currency='USD',provider='user_supplied_yahoo_export',
                    source_version=sha,available_at='',availability_basis='unknown',retrieved_at='',adjustment='unknown',data_mode='controlled_test' if synthetic else 'user_supplied'))
        with (folder/'normalized/prices.csv').open('x',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['Date','Close','Adj Close','source_line'])
            writer.writerows([str(r['date']),str(r['close']),str(r['adj_close']),r['line']] for r in parsed)
        load_csv(folder/'normalized/market.csv')
        for n, cutoff in enumerate(cutoffs):
            manifest = run(folder/'normalized/market.csv',symbol='VST',start=str(min(dates)),end=str(max(dates)),
                           cutoff=cutoff,calendar_path=folder/'raw/calendar.json' if calendar else None)
            save_manifest(manifest,folder/'validation'/f'time-machine-{n+1}.json')
    _write(folder/'quality_report.json',quality)
    fingerprints = {str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in folder.rglob('*') if p.is_file()}
    manifest = {'schema_version':VERSION,'processor_version':VERSION,'status':quality['status'],
                'source_declaration':'Synthetic fixture; NOT real VST data' if synthetic else 'User-provided Yahoo Finance CSV; origin not independently authenticated',
                'raw_sha256':sha,'processed_at':stamp,'downloaded_at':None,'available_at':None,'adjustment':'unknown',
                'start':str(min(dates)) if dates else None,'end':str(max(dates)) if dates else None,
                'record_count':len(parsed) if not errors else None,'warnings':warnings,'files_sha256':fingerprints,
                'price_policy':'market.csv uses Close only; prices.csv preserves separate Adj Close; neither labelled raw or total return',
                'strict_historical_replay_validated':False}
    _write(folder/'manifest.json',manifest)
    return folder, manifest


def cli(args):
    folder, manifest = import_csv(args.csv,root=args.output_root,calendar_path=args.calendar,
                                  cutoffs=args.cutoff,expected_sha256=args.expected_sha256)
    print(json.dumps({'snapshot':str(folder),'status':manifest['status'],'strict_historical_replay_validated':False}))
    return 1 if manifest['status']=='failed' else 0
