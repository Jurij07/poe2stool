# PoE2 Price Check

Ein **lokales** Price-Check-Tool für **Path of Exile 2** mit Web-Oberfläche.
Es liest nur, fragt die offizielle PoE2-Trade-API ab und zeigt die Ergebnisse an –
es automatisiert **keine** Spiel-Eingaben (kein Auto-Whisper, kein Macro). Damit
bleibt es konform zur GGG-API-Policy.

Zwei Modi:

- **Einzel-Item** – im Spiel mit `Strg+C` kopierten Item-Text einfügen → günstigste Listings.
- **Build-Code** – einen Path-of-Building-Code (langer Base64-String) einfügen oder als
  `.txt` hochladen → der ganze Build wird angezeigt **und** jedes Item automatisch bepreist,
  mit Einzelpreisen und einer Gesamtsumme in Divine.

---

## Setup

### 1. Abhängigkeiten installieren

```bash
pip install -r requirements.txt
```

(Empfohlen in einer virtuellen Umgebung: `python -m venv .venv && source .venv/bin/activate`)

### 2. config.json anlegen

```bash
cp config.example.json config.json
```

Dann `config.json` öffnen und ausfüllen:

| Feld                 | Bedeutung |
|----------------------|-----------|
| `poesessid`          | Dein Session-Cookie (siehe unten). |
| `league`             | `auto` = das Tool wählt **automatisch immer die neueste Liga** (aktuell „Aldur's…"). Alternativ einen festen Liga-Namen eintragen (z. B. `Standard`). |
| `realm`              | `poe2` (nicht ändern). |
| `user_agent`         | **Pflicht** laut GGG-Policy: beschreibender Name **+ Kontakt**, z. B. `MyPoe2PriceCheck/1.0 (kontakt: deine@mail.tld)`. |
| `exalted_per_divine` | Wechselkurs für die Divine-Umrechnung (wie viele Exalted = 1 Divine). Passe ihn an den aktuellen Markt an. |
| `currency_rates`     | Optionale Tabelle „Divine pro 1 Einheit“ für weitere Währungen (z. B. `chaos`). |
| `default_use_mods`   | Ob Rares standardmäßig mit Mod-Filtern gesucht werden. |

`config.json` ist in `.gitignore` und wird **nicht** eingecheckt – dein Cookie bleibt lokal.

### 3. POESESSID aus dem Browser holen

1. Im eingeloggten Browser auf <https://www.pathofexile.com> gehen (eingeloggt sein).
2. Entwicklertools öffnen (`F12`) → Reiter **Application** (Chrome) bzw. **Storage** (Firefox).
3. Unter **Cookies → https://www.pathofexile.com** den Eintrag **`POESESSID`** suchen.
4. Den **Value** kopieren und in `config.json` bei `poesessid` einsetzen.

> Das Cookie läuft nach einiger Zeit / beim Ausloggen ab. Wenn das Tool
> „POESESSID abgelaufen oder ungültig“ meldet, hier einfach einen frischen Wert holen.

### 4. Backend starten

```bash
python app.py
```

…oder mit Auto-Reload während der Entwicklung:

```bash
uvicorn app:app --reload --port 8000
```

### 5. Seite öffnen

Im Browser <http://127.0.0.1:8000> aufrufen.

---

## Benutzung

### Modus A – Einzel-Item
1. Item im Spiel mit `Strg+C` kopieren.
2. Reiter **Einzel-Item** → Text einfügen → **Preis prüfen**.
3. Du bekommst eine Tabelle mit Preis, Umrechnung in Divine, Verkäufer und Listing-Alter.
   - **Unique** → Suche per Name + Basistyp (präzise).
   - **Rare/Magic** → standardmäßig nur per Basistyp (Ballpark). Mit dem Schalter
     **„Mit Mod-Filtern“** werden erkannte Mods als Stat-Filter ergänzt.

### Modus B – Build-Code

**PoB-Code exportieren** (in Path of Building für PoE2):
`Import/Export` (oben) → Tab **Export** → **Generate** → **Copy to clipboard**.
Du bekommst einen langen Base64-String – den hier einfügen (oder als `.txt` hochladen).

1. Reiter **Build-Code** → Code einfügen → **Build laden & bepreisen**.
2. Oben erscheinen Klasse / Ascendancy / Level, darunter alle Gear-Slots mit
   Name, Rarity, Kurz-Mods und – während die Anfragen sequenziell durchlaufen –
   dem gefundenen Preis (Fortschritt: „Item 4 von 13 wird bepreist …“).
3. Am Ende steht die Gesamtsumme in Divine.
4. Jedes Item hat einen Status: **gefunden** / **Ballpark (Rare)** / **kein Treffer** / **beschädigt**.

---

## Wie es funktioniert (Kurzfassung)

- **PoB-Decode**: URL-safe-Base64 (`-`→`+`, `_`→`/`, Padding auffüllen) → Base64-Decode →
  zlib-Dekompression mit **tolerantem** Decoder. Eine kaputte/abgeschnittene Adler-32-Prüfsumme
  am Stream-Ende bricht den Decode **nicht** ab (Fallback auf rohen DEFLATE-Stream ohne
  Prüfsumme); kaputtes/abgeschnittenes XML wird per Regex notdürftig ausgelesen und
  betroffene Items werden klar als „beschädigt“ markiert.
- **Trade-API**: `POST /api/trade2/search/poe2/{league}` (`sort: price asc`) → erste 10 Hashes →
  `GET /api/trade2/fetch/{hashes}?query={id}&realm=poe2`.
- **Pflicht-Header** bei jeder Anfrage: `User-Agent` (mit Kontakt), `Cookie: POESESSID=…`,
  beim POST `Content-Type: application/json`.
- **Rate-Limits**: Anfragen laufen **sequenziell** durch eine Warteschlange. `X-Rate-Limit-*`-
  und `Retry-After`-Header werden gelesen; pro Endpoint sorgt ein Sliding-Window-Limiter dafür,
  dass die Limits eingehalten werden. Bei `429` wird gewartet und mit Backoff erneut versucht –
  nicht gespammt.
- **Mod-Matching**: `GET /api/trade2/data/stats` wird einmal geladen und gecacht; Item-Mods
  werden über normalisierten Text (Zahlen → `#`) auf Trade-Stat-IDs abgebildet.

---

## Grenzen / Einschränkungen

- **Cookie läuft ab**: `POESESSID` ist nur begrenzt gültig. Bei „abgelaufen“-Meldung neu setzen.
- **Preise schwanken**: Die Divine-Umrechnung nutzt den Kurs aus `config.json` (`exalted_per_divine`).
  Preise und Kurse ändern sich ständig – die Gesamtsumme ist eine **Schätzung**, kein Festpreis.
- **Rare-Matching ist ungenauer**: Rares werden per Basistyp (optional + Mod-Filter) gesucht.
  Das ist gröber als spezialisierte Tools wie **Exiled Exchange 2**; der „Ballpark“-Status
  weist genau darauf hin.
- **Lange Builds brauchen ein paar Sekunden**: Wegen der Rate-Limits werden die Items nacheinander
  bepreist. Ein voller Build kann daher einige Sekunden zum Durchpreisen brauchen.
- **Read-only**: Das Tool fragt nur ab und zeigt an. Es führt keinerlei Spiel-Automatisierung aus.
