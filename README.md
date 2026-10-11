# FlexNow seat watcher (JLU)

Notify-only. Watches PM-09 Diagnostik Kurs A (Kersting, Prfang 102579) and PM-11 Gesprächsführung Kurs C (Hermann, Prfang 102529), and pings when a seat frees up. Never enrolls by itself.

## Setup (once, on the machine that runs it)
    python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    playwright install chromium
    python watch.py --setup      # SHE types username + password; stored in the OS keychain only
Install the "ntfy" app on her phone and subscribe to the topic --setup prints.

## Run
    python watch.py --once       # test: lists all groups with free seats
    python watch.py              # keeps checking (~5 min day, ~15 min night)
    python watch.py --forget     # remove stored credentials

## When it pings
- Diagnostik: FlexNow -> An-/Abmeldung -> PM-09 -> put "Kurs C abmelden" AND "Kurs A anmelden" in the same basket -> Abschicken. Never abmelden from C first on its own.
- Gesprächsführung: PM-11 Klinische Psychologie -> Gesprächsführung Kurs C in the basket -> Abschicken (she is in no Gesprächsführung group yet, so nothing to abmelden).

Abmeldung deadline for the course is 09.11.2026, so seats can open until then.
FlexNow maintenance: Tue 20.10.2026 07:00-09:30 (watcher will just retry).
Add groups via config.json "targets" (prffach + prfang). Diagnostik (2947): A 102579, B 102565, C 102553, D 102539, E 102524. Gesprächsführung (2949): A 102558, B 102544, C 102529, D 102608, E 102594.
