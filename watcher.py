"""Padelhaus Bremen: read-only browser scan and Telegram alerts."""
import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from playwright.sync_api import TimeoutError
from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
CLUB = 'https://playtomic.com/clubs/padelhaus-bremen'
CLUBS = {
    'padelhaus': {'name': 'Padelhaus Bremen', 'url': CLUB, 'limit': 2000},
    'vamos': {'name': 'Vamos! Padelhalle Dreye',
              'url': 'https://playtomic.com/tenant/bceead86-14bf-4479-b945-26e264673098',
              'limit': 2800},
}
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
    parsed = datetime.strptime(value.strip(), '%I:%M %p' if value.strip().upper().endswith(('AM', 'PM')) else '%H:%M')    
    return parsed.hour * 60 + parsed.minute


def price_cents(text):
    match = re.search(r'€\s*(\d+(?:[.,]\d{2})?)', text)
    if not match:
        raise RuntimeError('Unbekannte Preisanzeige; Abbruch statt falschem Preis.')
    return int(Decimal(match.group(1).replace(',', '.')) * 100)


def qualifies(day, start, duration, cents, double=True, now=None, club="padelhaus"):
    now = now or datetime.now(BERLIN)
    starts = datetime.combine(day, datetime.min.time(), BERLIN) + timedelta(minutes=start)
    return (double and (club == "vamos" or day.weekday() in (5, 6))
            and (1020 if club == "vamos" and day.weekday() < 5 else 600) <= start <= 1260
            and duration in (60, 90) and 0 <= cents and cents * 60 < CLUBS[club]["limit"] * duration
            and starts > now)


def weekends(today, days):
    return [today + timedelta(days=i) for i in range(days + 1)
            if (today + timedelta(days=i)).weekday() in (5, 6)]


def scan(days, club="padelhaus"):
    config = CLUBS[club]
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={'width': 1360, 'height': 1000},
            locale='en-GB',
            timezone_id='Europe/Berlin'
        )
        page = context.new_page()
        page.set_default_timeout(15000)
        for day in [datetime.now(BERLIN).date() + timedelta(days=i) for i in range(days + 1)
                    if club == "vamos" or (datetime.now(BERLIN).date() + timedelta(days=i)).weekday() in (5, 6)]:
            # Resolve a shared tenant link once, then use the returned canonical URL.
            if '/tenant/' in config['url']:
                initial = page.goto(config['url'], wait_until='domcontentloaded', timeout=45000)
                if not initial or initial.status >= 400:
                    raise RuntimeError(f"{config['name']}: Clubseite nicht erreichbar.")
                if '/clubs/' not in page.url:
                    raise RuntimeError('Club-Link wurde nicht zur Buchungsseite weitergeleitet.')
                config['url'] = page.url.split('?')[0]
            response = page.goto(f"{config['url']}?date={day.isoformat()}", wait_until='domcontentloaded', timeout=45000)
            if not response or response.status >= 400:
                raise RuntimeError(f"{config['name']}: HTTP {response.status if response else 'keine Antwort'}; {page.title()}")
            page.get_by_role('heading', level=1).filter(has_text=config['name']).wait_for()
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
            # A visible legend is server-rendered and does not prove slots loaded.
            # Fail closed on an empty calendar: it can mean unavailable OR not loaded.
            try:
                page.locator(SLOTS).first.wait_for(state='attached', timeout=45000)
                page.get_by_text('Loading…', exact=True).wait_for(state='hidden', timeout=45000)
            except Exception as exc:
                raise RuntimeError(
                    f"{config['name']} – {day}: Keine auslesbaren Kalendertermine. "
                    "Laden fehlgeschlagen oder Tag vollständig belegt; "
                    "keine verlässliche Nullmeldung möglich."
                ) from exc
            records = page.locator(SLOTS).evaluate_all('''els => els.map(e => {
                let row = e;
                while(row && !row.querySelector('.truncate')) row = row.parentElement;
                return {id:e.getAttribute('data-slot-id'),
                  time:e.getAttribute('data-tracking-property-time'),
                  duration:e.getAttribute('data-tracking-property-duration'),
                  court:row?.querySelector('.truncate')?.textContent?.trim(),
                  type:row?.querySelector('.text-sm')?.textContent?.trim()};
            })''')
            print(f"{config['name']} – {day}: {len(records)} Kalendertermine ausgelesen.")
            if not any(r.get('court') and r.get('type') for r in records):
                raise RuntimeError('Kalendertermine vorhanden, aber Platzdaten nicht erkannt.')
            # Desktop rows identify court type. Mobile duplicate nodes have no row.
            candidates = {}
            for rec in records:
                if not rec['court']:
                    continue
                if not rec['type']:
                    raise RuntimeError('Platztyp konnte nicht bestätigt werden.')
                if 'Double' not in rec['type']:
                    continue
                start = minutes(rec['time'])
                if (1020 if club == "vamos" and day.weekday() < 5 else 600) <= start <= 1260 and int(rec['duration']) in (60, 90):
                    candidates.setdefault((rec['court'], start), rec)
            print(f"{config['name']} – {day}: {len(candidates)} mögliche Startzeiten im Zeitfenster.")
            for (court, start), rec in candidates.items():
                # Slot IDs are UI anchors, NOT reliable date identifiers.
                                # The selected date and popup labels are verified separately.
                loc = page.locator(f'[data-slot-id="{rec["id"]}"]').filter(visible=True).first
                accept = page.get_by_role('button', name='Accept All', exact=True)
                try:
                    accept.wait_for(state='visible', timeout=5000)
                except TimeoutError:
                    pass

                if accept.is_visible():
                    accept.click()
                    accept.wait_for(state='hidden')
                loc.click(position={"x": 2, "y": 20}, force=True, timeout=15000)
                try:
                    page.get_by_role('button', name='Close', exact=True).wait_for()
                except TimeoutError:
                    print("FEHLER BEIM ÖFFNEN:", court, rec['time'])
                    print("SEITE:", page.url)
                    page.screenshot(path="fehler.png", full_page=True)
                    raise

                popup = page.get_by_role('button', name='Close', exact=True).locator('..').locator('..')
                text = popup.inner_text()
                print("ERWARTET:", court, rec['time'])
                print("PREISFENSTER:", repr(text))                
                if ' '.join(court.split()) not in ' '.join(text.split()) or rec['time'] not in text:
                    raise RuntimeError('Preisanzeige gehört nicht zum ausgewählten Platz/Zeitpunkt.')
                options = page.locator(OPTIONS).evaluate_all('''els => els.filter(e=>e.getBoundingClientRect().width>0).map(e => ({duration:Number(e.getAttribute('data-tracking-property-activity-duration')),text:e.innerText}))''')
                if not options:
                    raise RuntimeError('Spieldauer und Preis fehlen.')
                for option in options:
                    duration, cents = option['duration'], price_cents(option['text'])
                    if qualifies(day, start, duration, cents, club=club):
                        results.append({'date': day.isoformat(), 'court': court, 'start': start,
                                        'duration': duration, 'cents': cents, 'club': club})
                page.get_by_role('button', name='Close', exact=True).click()
            time.sleep(1)  # modest, sequential request rate
        context.close()
        browser.close()
    return sorted(results, key=lambda r: (r['date'], r['start'], r['court'], r['duration']))


def key(offer):
    # Preserve hashes from the original Padelhaus-only version.
    identity = dict(offer)
    if identity.get('club') == 'padelhaus':
        identity.pop('club')
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def offer_line(offer):
    start = offer['start']
    end = start + offer['duration']
    day = date.fromisoformat(offer['date'])
    weekday = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][day.weekday()]
    config = CLUBS[offer.get('club', 'padelhaus')]
    price = f'{offer["cents"] / 100:.2f}'.replace('.', ',')
    hourly = f'{offer["cents"] * 60 / offer["duration"] / 100:.2f}'.replace('.', ',')
    return (f"{config['name']}\n" + f'{weekday}, {day:%d.%m.%Y} · {start//60:02}:{start%60:02}–{end//60:02}:{end%60:02}\n'
            f'{offer["court"]} · Doppel · {offer["duration"]} Min.\n'
            f"{price} € gesamt · {hourly} €/Std.\n{config['url']}?date={day.isoformat()}")


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
        offers = scan(days, 'padelhaus')
    except Exception:
        if mode == 'check':
            today = datetime.now(BERLIN).date().isoformat()
            if state.get('last_error') != today:
                send('Padel-Wecker: Prüfung fehlgeschlagen. Das bedeutet NICHT, dass alles belegt ist. Bitte die Ausgabe des Prüflaufs ansehen.')
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
        send('🎾 Neue passende Padelplätze\n\n' + '\n\n'.join(map(offer_line, batch)) + '\n\nBeim Prüfen frei; bis zur Buchung kann sich das ändern.')
        for offer in batch:
            state['sent'][key(offer)] = offer['date']
        STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')
    
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
            detail = str(exc)
            for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_PAIR_CODE', 'TELEGRAM_CHAT_ID'):
                value = os.environ.get(name, '')
                if value:
                    detail = detail.replace(value, '***')
            print(f'Prüfung abgebrochen ({type(exc).__name__}): {detail}')
        raise SystemExit(1)
        

