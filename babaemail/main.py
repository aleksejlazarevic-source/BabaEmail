"""Daily email layer; Lidl is read exclusively from its weekly JSON cache."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from pathlib import Path
import sys

from .mailer import send_email
from .models import SourceResult, SourceStatus
from .render import ROOT, render
from .select import Selection, load_preferences, select
from .store import LidlCache, failure, load_lidl, write_history
from .verify import Verification, verify

STORE_KEYS = ('lidl', 'hofer', 'mercator', 'spar', 'tus')


def fetch_live(key: str, today: date) -> SourceResult:
    # Lazy imports keep an offline run independent of scraper dependencies.
    try:
        from .sources.hofer import Hofer
        from .sources.mercator import Mercator
        from .sources.spar import Spar
        from .sources.tus import Tus
        return {'hofer': Hofer, 'mercator': Mercator, 'spar': Spar, 'tus': Tus}[key]().fetch(today)
    except Exception as exc:
        # One broken adapter must not suppress the other stores or the banner.
        return failure(key, f'{type(exc).__name__}: {exc}')


def build_report(today: date, cache: LidlCache, verified: list[Verification], selection: Selection) -> str:
    lines = [f'BabaEmail {today}', f'Lidl cache: {cache.path or "none"}',
             'held = quarantined extracted items (subset of rejected-by-verify); ingestion-held listed separately.']
    for result in verified:
        key = result.source.retailer
        shipped = [d for d in selection.items if d.retailer == key]
        cuts = [c for c in selection.cuts if c.deal.retailer == key]
        lines.extend(['', f'{key}: status={result.source.status.value}; extracted={len(result.source.items)}; '
                      f'verified={len(result.accepted)}; shipped={len(shipped)}; '
                      f'held={len(result.source.quarantined)}; rejected-by-verify={len(result.rejected)}; '
                      f'cut-by-preferences={len(cuts)}', f'error: {result.source.error or "none"}'])
        counts = Counter(reason.split(':', 1)[0] for rejected in result.rejected for reason in rejected.reasons)
        lines.append(f'verify reason counts: {dict(counts)}')
        lines.append(f'preference reason counts: {dict(Counter(c.reason for c in cuts))}')
        for rejected in result.rejected:
            lines.append(f'REJECT p{rejected.deal.source_page or "?"} {rejected.deal.name}: ' + '; '.join(rejected.reasons))
        for cut in cuts:
            lines.append(f'CUT {cut.deal.name} [{cut.deal.category}]: {cut.reason}')
        for deal in shipped:
            lines.append(f'SHIP {deal.name}: {deal.price_new}; category={deal.category}; '
                         f'condition={deal.condition or "none"}; validity={deal.valid_from}..{deal.valid_to}')
        for notice in result.source.notices:
            lines.append(f'NOTICE {notice.kind}: proven={notice.proven}; day={notice.day}; '
                         f'reason={notice.reason}; text={notice.text}')
    lines.extend(['', f'ingestion-held={len(cache.held_records)}', *cache.held_records])
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Write previews; never connect to SMTP')
    parser.add_argument('--send', action='store_true', help='Explicitly send (ignored with --dry-run)')
    parser.add_argument('--date', type=date.fromisoformat, default=date.today())
    parser.add_argument('--lidl-json', type=Path)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--preferences', type=Path, default=ROOT / 'preferences.yaml')
    parser.add_argument('--out', type=Path, default=ROOT / 'out')
    args = parser.parse_args(argv)
    preferences = load_preferences(args.preferences)
    cache = load_lidl(args.date, args.out, args.lidl_json)
    sources = [cache.source]
    for key in STORE_KEYS[1:]:
        print(f'{key}: {"offline" if args.offline else "fetching"}', flush=True)
        sources.append(failure(key, 'Offline: live fetch intentionally skipped', SourceStatus.BLOCKED)
                       if args.offline else fetch_live(key, args.date))
    # All extracted items, including stores disabled in preferences, are verified
    # before the first preference is applied.
    verified = [verify(source, args.date, preferences['lookahead_days']) for source in sources]
    selection = select(verified, preferences)
    report = build_report(args.date, cache, verified, selection)
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f'report-{args.date}.txt'
    report_path.write_text(report, 'utf-8')
    html, text = render(selection, sources, preferences, args.date)
    html_path, text_path = (args.out / f'email-{args.date}.{suffix}' for suffix in ('html', 'txt'))
    html_path.write_text(html, 'utf-8')
    text_path.write_text(text, 'utf-8')
    unchecked = {s['key'] for s in preferences['stores'] if s.get('checked', True) is False}
    failed = any(not s.contributes for s in sources if s.retailer not in unchecked)
    print(f'Wrote {html_path}, {text_path}, {report_path}; {len(selection.items)} items; HTML {len(html.encode("utf-8"))} bytes')
    if args.send and not args.dry_run:
        try:
            send_email(f'Akcije za {args.date.day}. {args.date.month}.', html, text,
                       alert=report if failed else None)
            write_history(args.out / 'history', args.date, selection.items)
        except Exception as exc:
            with report_path.open('a', encoding='utf-8') as file:
                file.write(f'\nSEND FAILED: {type(exc).__name__}: {exc}\n')
            print(f'Email delivery failed: {exc}', file=sys.stderr)
            return 2
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
