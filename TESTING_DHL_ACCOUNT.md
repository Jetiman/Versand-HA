# Test: DHL-Konto-Login (automatische Sendungserkennung)

> **Nur zum Testen.** Dieser Zweig (`feature/dhl-account`) fügt eine
> optionale DHL-Konto-Anmeldung hinzu, damit die Sendungen deines DHL-Kontos
> automatisch in der Liste auftauchen – ohne dass du jede Nummer von Hand
> einträgst. Das Konzept ist aus dem Pull Request #1 übernommen, aber sauber
> an den aktuellen Stand angepasst (nicht 1:1 kopiert).
>
> `master` bleibt davon unberührt.

## Was es macht

1. Du öffnest einen DHL-Login-Link und meldest dich mit deinem DHL-Konto an.
2. DHL leitet danach auf eine `dhllogin://…`-Adresse weiter. Diese Adresse
   fügst du in Home Assistant ein.
3. Home Assistant tauscht den enthaltenen Code gegen ein Sitzungs-Token
   (`id_token` / `refresh_token`) und speichert **nur dieses Token** im
   Config-Entry – **kein Passwort**.
4. Bei jeder Aktualisierung fragt die Integration die (nicht archivierten)
   Sendungen des Kontos ab und hängt deren Nummern an die verfolgte Liste an
   (Anbieter automatisch = DHL). Das Token wird bei Bedarf selbst erneuert.

## Wichtige Hinweise

- Die OAuth-Parameter (Client-ID, Login-URL) stammen aus der DHL-iOS-App.
  Wenn DHL sie ändert, funktioniert der Login nicht mehr – das ist genau
  der Grund für diesen Testzweig.
- Die Funktion ist **standardmäßig aus**. Nichts ändert sich, solange du den
  Haken nicht setzt.
- Laut Projekt-Historie hat ein früherer Versuch dieser Konto-Abfrage leere
  Ergebnisse geliefert. Ob der `dhli`-Cookie-Trick heute noch Sendungen
  zurückgibt, ist offen – bitte berichte, was passiert.

## Installation des Testzweigs

**Variante A – Dateien manuell kopieren (am einfachsten zum Zurückrollen):**

1. `master` in HACS ist installiert – mach vorher ein Backup / notiere dir
   die Version.
2. Lade den Zweig als ZIP:
   `https://github.com/Jetiman/Versand-HA/archive/refs/heads/feature/dhl-account.zip`
3. Ersetze den Ordner
   `config/custom_components/paketverfolgung/`
   komplett durch den aus dem ZIP (`custom_components/paketverfolgung/`).
4. Home Assistant neu starten.

**Variante B – HACS „Redownload" mit Zweigauswahl:**

1. HACS → Paketverfolgung → 3-Punkte-Menü → *Redownload*.
2. Bei „Select version" den Zweig `feature/dhl-account` wählen
   (erscheint nur, wenn „Show beta versions" bzw. Zweige aktiviert sind).
3. Home Assistant neu starten.

**Zurückrollen:** In HACS wieder die Version `1.10.1` (oder `master`) wählen
und neu starten. Der Config-Entry bleibt kompatibel; das gespeicherte
DHL-Token wird von `master` einfach ignoriert.

## Testen

1. **Einstellungen → Geräte & Dienste → Paketverfolgung → Konfigurieren.**
2. Im Formular „Sendungsnummern" unten den neuen Haken setzen:
   **„DHL-Konto verbinden und Sendungen automatisch erkennen (Test)"** →
   *Absenden*.
3. Es öffnet sich der Schritt **„DHL-Konto verbinden"**:
   - Öffne den angezeigten Link in einem Browser (Handy oder PC).
   - Melde dich mit deinem DHL-Konto an.
   - Nach dem Login zeigt der Browser eine Fehlerseite / „Seite kann nicht
     geöffnet werden" für eine `dhllogin://…`-Adresse. **Das ist richtig so.**
     Kopiere die komplette Adresse aus der Adresszeile.
     - Chrome Android: ggf. lange auf die Adresszeile tippen → „Bearbeiten"
       → alles markieren → kopieren.
   - Füge die Adresse in das Feld **„Weiterleitungs-Adresse (dhllogin://…)"**
     ein → *Absenden*.
4. Die Integration lädt neu. Nach spätestens einem Aktualisierungsintervall
   (Standard 15 min, oder „Jetzt aktualisieren" im Panel) sollten die
   Konto-Sendungen in der Liste erscheinen.

## Was beobachten

- **Sensor `sensor.paketverfolgung_dhl_konto_erkennung`** (Diagnose):
  - `Aus` – Haken nicht gesetzt
  - `Kein DHL-Login hinterlegt` – Haken an, aber Login fehlt
  - `N Sendung(en) erkannt` – Abfrage lief, N Sendungen gefunden
    (`0 Sendung(en) erkannt` = Login ok, aber DHL gibt nichts zurück)
  - eine Fehlermeldung – Login abgelaufen / abgelehnt / Netzwerkfehler
- **Protokoll** (Einstellungen → System → Protokolle, oder
  `home-assistant.log`), Filter `paketverfolgung`:
  - `DHL-Konto -> [...]` (Debug) – die roh gefundenen Sendungs-IDs
  - `DHL account discovery response: {...}` (Debug) – die komplette Antwort
  - `DHL-Kontoabfrage fehlgeschlagen: ...` (Warnung)

Debug-Logging einschalten (in `configuration.yaml` oder per Dienst
`logger.set_level`):

```yaml
logger:
  logs:
    custom_components.paketverfolgung: debug
```

## Bitte zurückmelden

- Kommt der Login-Schritt durch (Token wird geholt)?
- Was steht im Diagnose-Sensor?
- Falls `0 Sendung(en)`: den (anonymisierten) Inhalt von
  `DHL account discovery response:` – dann sieht man, ob DHL die Sendungen in
  einem anderen Feld liefert.
