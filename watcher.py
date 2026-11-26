"""Padelhaus Bremen: read-only browser scan and Telegram alerts."""
import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
CLUB = 'https://playtomic.com/clubs/padelhaus-bremen'
STATE = Path('state.json')
SLOTS = '[data-tracking-action-name="Slot Selected"][data-slot-id]'
OPTIONS = '[data-tracking-action-name="Activity Duration Changed"]'


def secret(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'GitHub Secret fehlt: {name}')
    return value


def telegram(method, data):
    # Never log the URL: it contains the bot token.
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{secret("TELEGRAM_BOT_TOKEN")}/{method}',
        data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
    except Exception:
        raise RuntimeError('Telegram-Anfrage fehlgeschlagen. Token, Chat und Verbindung prüfen.') from None
    if not result.get('ok'):
        raise RuntimeError('Telegram hat die Anfrage abgelehnt.')
    return result['result']


def send(text, chat_id=None):
    telegram('sendMessage', {'chat_id': chat_id or secret('TELEGRAM_CHAT_ID'),
                            'text': text, 'link_preview_options': {'is_disabled': True}})


def setup():
    code = secret('TELEGRAM_PAIR_CODE')
    if len(code) < 16:
        raise RuntimeError('Der geheime Kopplungscode muss mindestens 16 Zeichen lang sein.')
    bot = telegram('getMe', {})
    if bot.get('username', '').lower() != 'padelwecker_bot':
        raise RuntimeError('Token gehört nicht zu @padelwecker_bot.')
    updates = telegram('getUpdates', {'timeout': 0, 'limit': 100})
    ids = {u['message']['chat']['id'] for u in updates
           if u.get('message', {}).get('chat', {}).get('type') == 'private'
           and u['message'].get('text', '').strip() == '/start ' + code}
    if len(ids) != 1:
        raise RuntimeError('Keine eindeutige Kopplung. Sende deinem Bot /start gefolgt vom geheimen Code und starte setup erneut.')
    chat_id = ids.pop()
    send(f'✅ Telegram verbunden. Deine Chat-ID: {chat_id}\n'
         'Trage sie in GitHub als Secret TELEGRAM_CHAT_ID ein. '
         'Dies ist eine Testnachricht; die Platzüberwachung ist noch nicht eingeschaltet.', chat_id)
    print('Kopplung erfolgreich. Die Chat-ID steht ausschließlich in deiner Telegram-Nachricht.')


def minutes(value):
    parsed = datetime.strptime(value.strip(), '%I:%M %p')
    return parsed.hour * 60 + parsed.minute


def price_cents(text):
    match = re.search(r'€\s*(\d+(?:[.,]\d{2})?)', text)
    if not match:
        raise RuntimeError('Unbekannte Preisanzeige; Abbruch statt falschem Preis.')
    return int(Decimal(match.group(1).replace(',', '.')) * 100)


def qualifies(day, start, duration, cents, double=True, now=None):
    now = now or datetime.now(BERLIN)
    starts = datetime.combine(day, datetime.min.time(), BERLIN) + timedelta(minutes=start)
    return (double and day.weekday() in (5, 6) and 600 <= start <= 1260
            and duration in (60, 90) and 0 <= cents and cents * 60 < 2000 * duration
            and starts > now)


def weekends(today, days):
    return [today + timedelta(days=i) for i in range(days + 1)
            if (today + timedelta(days=i)).weekday() in (5, 6)]


def scan(days):
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1360, 'height': 1000},
                                      locale='en-GB', timezone_id='Europe/Berlin')
        page = context.new_page()
        page.set_default_timeout(15000)
        for day in weekends(datetime.now(BERLIN).date(), days):
            response = page.goto(f'{CLUB}?date={day.isoformat()}', wait_until='domcontentloaded', timeout=45000)
            if not response or response.status >= 400:
                raise RuntimeError('Playtomic-Seite konnte nicht geladen werden; keine Verfügbarkeitsaussage möglich.')
            page.get_by_role('heading', name='Book a court in Padelhaus Bremen', exact=True).wait_for()
            deny = page.locator('#deny')
            if deny.is_visible():
                deny.click()
            elif page.locator('#more').is_visible():
                page.locator('#more').click()
                page.locator('#deny').click()
            # Ensure hydration has created the actual calendar before reading it.
            page.locator('input[type="date"]').first.wait_for(state='attached')
            page.get_by_text('Loading…', exact=True).wait_for(state='hidden', timeout=45000)
            page.get_by_text('available', exact=True).wait_for(state='visible', timeout=15000)
            if page.locator('input[type="date"]').first.input_value() != day.isoformat():
                raise RuntimeError('Angezeigtes Datum stimmt nicht mit dem angefragten Tag überein.')
            if not page.get_by_text('available', exact=True).count():
                raise RuntimeError('Kalender-Legende fehlt; Seitenaufbau hat sich geändert.')
            records = page.locator(SLOTS).evaluate_all('''els => els.map(e => {
                let row = e;
                while(row && !row.querySelector('.truncate')) row = row.parentElement;
                return {id:e.getAttribute('data-slot-id'),
                  time:e.getAttribute('data-tracking-property-time'),
                  duration:e.getAttribute('data-tracking-property-duration'),
                  court:row?.querySelector('.truncate')?.textContent?.trim(),
                  type:row?.querySelector('.text-sm')?.textContent?.trim()};
            })''')
            # Desktop rows identify court type. Mobile duplicate nodes have no row.
            candidates = {}
            for rec in records:
                if not rec['court']:
                    continue
                if not rec['type'] or 'Double' not in rec['type']:
                    raise RuntimeError('Doppelplatz-Typ konnte nicht bestätigt werden.')
                start = minutes(rec['time'])
                if 600 <= start <= 1260 and int(rec['duration']) in (60, 90):
                    candidates.setdefault((rec['court'], start), rec)
            print(f'{day}: {len(candidates)} mögliche Startzeiten im Zeitfenster.')
            for (court, start), rec in candidates.items():
                # Slot IDs are UI anchors, NOT reliable date identifiers.
                # The selected date and popup labels are verified separately.
                loc = page.locator(f'[data-slot-id="{rec["id"]}"]').filter(visible=True).first
                loc.click(force=True)  # durations overlap in the same visible grid cell
                page.get_by_role('button', name='Close', exact=True).wait_for()
                popup = page.get_by_role('button', name='Close', exact=True).locator('..').locator('..')
                text = popup.inner_text()
                if court not in text or rec['time'] not in text:
                    raise RuntimeError('Preisanzeige gehört nicht zum ausgewählten Platz/Zeitpunkt.')
                options = page.locator(OPTIONS).evaluate_all('''els => els.filter(e=>e.getBoundingClientRect().width>0).map(e => ({duration:Number(e.getAttribute('data-tracking-property-activity-duration')),text:e.innerText}))''')
                if not options:
                    raise RuntimeError('Spieldauer und Preis fehlen.')
                for option in options:
                    duration, cents = option['duration'], price_cents(option['text'])
                    if qualifies(day, start, duration, cents):
                        results.append({'date': day.isoformat(), 'court': court, 'start': start,
                                        'duration': duration, 'cents': cents})
                page.get_by_role('button', name='Close', exact=True).click()
            time.sleep(1)  # modest, sequential request rate
        context.close()
        browser.close()
    return sorted(results, key=lambda r: (r['date'], r['start'], r['court'], r['duration']))


def key(offer):
    return hashlib.sha256(json.dumps(offer, sort_keys=True).encode()).hexdigest()


def offer_line(offer):
    start = offer['start']
    end = start + offer['duration']
    day = date.fromisoformat(offer['date'])
    weekday = 'Sa' if day.weekday() == 5 else 'So'
    price = f'{offer["cents"] / 100:.2f}'.replace('.', ',')
    hourly = f'{offer["cents"] * 60 / offer["duration"] / 100:.2f}'.replace('.', ',')
    return (f'{weekday}, {day:%d.%m.%Y} · {start//60:02}:{start%60:02}–{end//60:02}:{end%60:02}\n'
            f'{offer["court"]} · Doppel · {offer["duration"]} Min.\n'
            f'{price} € gesamt · {hourly} €/Std.\n{CLUB}?date={day.isoformat()}')


def run(mode):
    if mode == 'setup':
        setup()
        return
    days = int(os.environ.get('BOOKING_DAYS', '0'))
    if not 1 <= days <= 90:
        raise RuntimeError('BOOKING_DAYS auf den in der App bestätigten Buchungshorizont setzen (1–90).')
    if mode == 'check':
        secret('TELEGRAM_BOT_TOKEN')
        secret('TELEGRAM_CHAT_ID')
    state = json.loads(STATE.read_text()) if STATE.exists() else {'sent': {}}
    try:
        offers = scan(days)
    except Exception:
        if mode == 'check':
            today = datetime.now(BERLIN).date().isoformat()
            if state.get('last_error') != today:
                send('⚠️ Padel-Wecker: Die Prüfung ist fehlgeschlagen. Das bedeutet NICHT, dass alles belegt ist. Bitte den letzten GitHub-Actions-Lauf prüfen.')
                state['last_error'] = today
                STATE.write_text(json.dumps(state, indent=2) + '\n')
        raise
    print(f'{len(offers)} passende Angebote gefunden. Modus: {mode}.')
    if mode == 'diagnose':
        for offer in offers:
            print(offer_line(offer))
        return
    today = datetime.now(BERLIN).date().isoformat()
    state['sent'] = {k: v for k, v in state['sent'].items() if v >= today}
    fresh = [o for o in offers if key(o) not in state['sent']]
    for i in range(0, len(fresh), 8):
        batch = fresh[i:i+8]
        send('🎾 Neue passende Plätze – Padelhaus Bremen\n\n' + '\n\n'.join(map(offer_line, batch)) + '\n\nBeim Prüfen frei; bis zur Buchung kann sich das ändern.')
        for offer in batch:
            state['sent'][key(offer)] = offer['date']
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
    if not state.get('first_success'):
        send(f'✅ Erste vollständige Prüfung erfolgreich: nächste {days} Tage, Samstag/Sonntag, Doppel, Start 10–21 Uhr, unter 20 €/Std.\n{len(offers)} passende Angebote.\nAutomatische Läufe starten erst mit der GitHub-Variable ENABLED=true.')
        state['first_success'] = True
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'diagnose', 'check'])
    try:
        run(parser.parse_args().mode)
    except Exception as exc:
        # Avoid Playwright dumps and HTTP exception strings leaking credentials.
        if isinstance(exc, RuntimeError):
            print(str(exc))
        else:
               print(f'Prüfung abgebrochen ({type(exc).__name__}): {exc}')
        raise SystemExit(1)
