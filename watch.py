#!/usr/bin/env python3
"""FlexNow (JLU Gießen) seat watcher: notify-only, never enrolls.

Logs into FlexNow 2, opens An-/Abmeldung -> each watched Prüfungsfach (config
"targets"), reads the "Kapazität" line of the watched groups and sends a push
when a seat is free. Taking the seat (Ummeldung) is done by hand.

Credentials live in the OS keychain (macOS Keychain / Windows Credential
Manager) via `keyring`. On a server (Docker/Coolify) they come from the env
vars FLEXNOW_USER / FLEXNOW_PASS instead, plus NTFY_TOPIC for the push topic.

  python watch.py --setup      # ask for username + password, store in keychain, create ntfy topic
  python watch.py --once       # one check, print all groups, exit
  python watch.py              # loop: check every ~5 min (15 min at night)
  python watch.py --forget     # delete stored credentials
"""
import argparse, getpass, json, os, random, re, secrets, subprocess, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SERVICE = "flexnow-jlu-watch"
CONFIG = Path(__file__).with_name("config.json")
LOGIN_URL = "https://www.uni-giessen.de/de/studium/waehrend/ecampus/flexnow/"

DEFAULT_CONFIG = {
    # Ids as seen in FlexNow on 11.10.2026. prffach = Prüfungsfach, prfang = the group.
    "targets": [
        {"prffach": "2947", "prfang": "102579", "label": "Diagnostik Kurs A (Kersting)",
         "hint": "Kurs C abmelden + Kurs A anmelden in EINEM Korb, dann Abschicken."},
        {"prffach": "2949", "prfang": "102529", "label": "Gesprächsführung Kurs C (Hermann)",
         "hint": "PM-11 Klinische Psychologie: Gesprächsführung Kurs C in den Korb, dann Abschicken."},
    ],
    "ntfy_topic": "",
    "interval_day_s": 300,
    "interval_night_s": 900,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    return cfg


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def setup():
    import keyring
    cfg = load_config()
    user = input("FlexNow Kennung: ").strip().lower()
    pw = getpass.getpass("FlexNow Kennwort (wird nicht angezeigt): ")
    keyring.set_password(SERVICE, "username", user)
    keyring.set_password(SERVICE, user, pw)
    if not cfg["ntfy_topic"]:
        cfg["ntfy_topic"] = "fn-" + secrets.token_urlsafe(12)
    save_config(cfg)
    print("\nGespeichert im Schlüsselbund.")
    print(f"Push: App 'ntfy' installieren und dieses Thema abonnieren: {cfg['ntfy_topic']}")
    print("Test: python watch.py --once")


def forget():
    import keyring
    user = keyring.get_password(SERVICE, "username")
    if user:
        keyring.delete_password(SERVICE, user)
        keyring.delete_password(SERVICE, "username")
    print("Zugangsdaten gelöscht.")


def creds():
    if os.environ.get("FLEXNOW_USER") and os.environ.get("FLEXNOW_PASS"):
        return os.environ["FLEXNOW_USER"].strip().lower(), os.environ["FLEXNOW_PASS"]
    import keyring
    user = keyring.get_password(SERVICE, "username")
    pw = keyring.get_password(SERVICE, user) if user else None
    if not (user and pw):
        sys.exit("Keine Zugangsdaten. Erst: python watch.py --setup")
    return user, pw


def notify(cfg, title, msg):
    print(f"[{datetime.now():%H:%M:%S}] {title}: {msg}")
    if cfg.get("ntfy_topic"):
        try:
            body = {"topic": cfg["ntfy_topic"], "title": title, "message": msg, "priority": 5, "tags": ["rotating_light"]}
            req = urllib.request.Request(  # JSON publish: HTTP headers can't carry umlauts in the title
                "https://ntfy.sh/", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("  ntfy failed:", e)
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Glass"'])


def parse_free(text):
    """'Es sind insgesamt 33 Plätze verfügbar. Davon sind 33 Plätze bereits belegt und 0 noch frei.' -> (33, 33, 0)"""
    t = " ".join(text.split())
    m = re.search(r"insgesamt (\d+) Pl\S+ verf\S+ Davon sind (\d+) Pl\S+ bereits belegt und (\d+) noch frei", t)
    return tuple(int(x) for x in m.groups()) if m else None


READ_BLOCKS = """() => [...document.querySelectorAll('#containerListeTeilprf .prfangBlock')].map(b => ({
    prfang: (b.querySelector('input[name=Prfang]') || {}).value || '',
    title: (b.querySelector('#PrfAngLvTitel') || {}).innerText || '',
    cap: (b.querySelector('p.Kapazitaet') || {}).innerText || '',
    mine: b.classList.contains('angemeldet'),
}))"""


def read_fach(page, prffach, wanted):
    """Open one Prüfungsfach and return its group blocks; expands collapsed Teilprüfungen until `wanted` show up."""
    fach = page.locator(f'a.studiumAnchor[onclick*=";BAP;{prffach};"]').first
    fach.wait_for(state="attached", timeout=30000)
    fach.evaluate("el => el.click()")  # the link sits in a collapsed tree node, so a normal click fails
    page.wait_for_function(
        f"() => {{ const w = document.querySelector('#containerListeTeilprf #wahlpflicht'); return w && w.value.includes(';{prffach};'); }}",
        timeout=30000)
    try:
        page.wait_for_selector("#containerListeTeilprf .prfangBlock", timeout=15000)
    except PWTimeout:
        pass
    rows = page.evaluate(READ_BLOCKS)
    for _ in range(12):  # expand collapsed Teilprüfungen one by one; the list re-renders after each click
        if wanted <= {r["prfang"] for r in rows}:
            break
        heads = page.locator("#containerListeTeilprf p.teilprfBezeichner:has(img.plusTeilPrf)")
        if heads.count() == 0:
            break
        heads.first.evaluate("el => el.click()")
        page.wait_for_timeout(2500)
        rows = page.evaluate(READ_BLOCKS)
    return rows


def check(cfg):
    """Returns list of dicts: prfang, title, total, taken, free (None if no capacity line)."""
    user, pw = creds()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="de-DE")
        try:
            page.goto(LOGIN_URL, timeout=45000)
            page.fill("#username", user)
            page.fill("#password", pw)
            page.click("#logindata [type=submit]")
            page.wait_for_selector("#nav_anab a.selectable", timeout=45000)
            page.click("#nav_anab a.selectable")
            rows = []
            for prffach in dict.fromkeys(t["prffach"] for t in cfg["targets"]):
                rows += read_fach(page, prffach, {t["prfang"] for t in cfg["targets"] if t["prffach"] == prffach})
        finally:
            try:  # end the session cleanly
                page.locator("a.selectable.last").first.click(timeout=5000)
            except Exception:
                pass
            browser.close()
    out = []
    for r in rows:
        cap = parse_free(r["cap"]) if r["cap"] else None
        title = re.sub(r"\s+", " ", r["title"]).replace("Prüfung: - ", "").strip()
        out.append({"prfang": r["prfang"], "title": title, "mine": r["mine"],
                    "total": cap[0] if cap else None, "taken": cap[1] if cap else None,
                    "free": cap[2] if cap else None})
    return out


def report(rows):
    for r in rows:
        tag = "ANGEMELDET" if r["mine"] else (f"{r['free']} frei / {r['total']}" if r["free"] is not None else "?")
        print(f"  {r['prfang']:>7}  {tag:<14} {r['title'][:90]}")


def loop(cfg):
    alerted = set()
    fails = 0
    first = True
    while True:
        try:
            rows = check(cfg)
            fails = 0
            print(f"[{datetime.now():%d.%m. %H:%M:%S}] check ok")
            if first:
                report(rows)
                notify(cfg, "FlexNow-Watcher läuft", "Erster Check ok. Push kommt, sobald ein Platz frei wird.")
                first = False
            by_id = {r["prfang"]: r for r in rows}
            for t in cfg["targets"]:
                r = by_id.get(t["prfang"])
                if r is None:
                    print(f"  nicht gefunden: {t['label']} ({t['prfang']})")
                elif r["free"] and r["free"] > 0:
                    if t["prfang"] not in alerted:
                        notify(cfg, f"FlexNow: {t['label']}: {r['free']} Platz frei!", "Jetzt FlexNow öffnen. " + t["hint"])
                        alerted.add(t["prfang"])
                else:
                    alerted.discard(t["prfang"])
        except Exception as e:
            fails += 1
            print(f"[{datetime.now():%H:%M:%S}] Fehler ({fails}): {e}")
            if fails == 3:
                notify(cfg, "FlexNow-Watcher: Fehler", "3 Checks hintereinander fehlgeschlagen. Kennwort geändert oder Wartung?")
        night = datetime.now().hour < 6
        base = cfg["interval_night_s"] if night else cfg["interval_day_s"]
        time.sleep(base + random.randint(-45, 45) + min(fails, 6) * 120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--forget", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.setup:
        return setup()
    if a.forget:
        return forget()
    cfg = load_config()
    if a.once:
        report(check(cfg))
        return
    loop(cfg)


if __name__ == "__main__":
    main()
