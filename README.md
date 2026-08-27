# Paketverfolgung für Home Assistant

Zeigt den Status deiner Paketsendungen als Sensoren und als eigene Seitenleisten-Oberfläche in Home Assistant an. Zwei Wege, die sich beliebig kombinieren lassen:

- **Sendungsnummern** – du trägst Nummern ein, der Anbieter (**DHL** oder **DPD**) wird pro Nummer automatisch erkannt.
- **DPD-Konto** – Login mit deinem myDPD-Konto, alle Sendungen werden automatisch erkannt.

## Installation über HACS

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. Dieses Repository als URL eintragen, Kategorie **Integration** wählen, hinzufügen.
3. „Paketverfolgung“ in HACS suchen und installieren.
4. Home Assistant neu starten.

**Einstellungen → Geräte & Dienste → Integration hinzufügen → „Paketverfolgung“** ausführen und wählen, was du hinzufügen möchtest. „Sendungsnummern“ und „DPD-Konto“ können parallel eingerichtet sein.

> ⚠️ **Hinweis:** Alle genutzten Schnittstellen sind **inoffiziell** (keine dokumentierten APIs). Sie können sich jederzeit ohne Vorwarnung ändern.

## Sendungsnummern (DHL & DPD)

1. „Sendungsnummern (DHL & DPD)“ wählen.
2. Eine oder mehrere Nummern eingeben (nach jeder Nummer Enter). Der Schritt kann leer übersprungen werden.
3. Optional deine **PLZ** hinterlegen – manche DPD-Sendungen sind ohne Empfänger-PLZ nicht öffentlich abrufbar.

Bei der nächsten Aktualisierung wird jede Nummer bei DHL und – falls dort nichts gefunden wird – bei DPD nachgeschlagen. Der erkannte Anbieter wird gemerkt und danach nur noch dieser abgefragt.

- **DHL:** öffentliche Sendungsverfolgungs-Suche (kein Login), inkl. komplettem Verlauf und Zustellzeitfenster.
- **DPD:** öffentliche „Parcel Life Cycle“-Verfolgung von tracking.dpd.de, inkl. Verlauf.

**Nummern später hinzufügen/entfernen:** Zahnrad-Symbol beim Eintrag „Sendungsnummern“ – dort auch PLZ und Aktualisierungsintervall (Standard: 15 Minuten). Oder über den Dienst `paketverfolgung.add_tracking_number` bzw. das Eingabefeld in der Oberfläche.

Eine Nummer, zu der (noch) kein Anbieter Daten liefert, bleibt mit dem Status „Noch keine Daten“ bestehen, bis du sie entfernst oder Daten verfügbar sind.

## DPD-Konto

Meldet sich mit deinem **myDPD-Konto** an (SOAP-API der offiziellen DPD-App „Paketnavigator“) und zeigt automatisch **alle** Sendungen (gesendet, empfangen, Retouren) an – keine manuelle Eingabe nötig. Der vollständige Verlauf wird zusätzlich über die öffentliche DPD-Verfolgung nachgeladen; bei geschützten Sendungen hilft die in den Optionen hinterlegte PLZ.

Das Passwort wird nur lokal in Home Assistant gespeichert (wie bei jeder anderen Cloud-Integration).

## Was wird angezeigt?

Pro Sendung ein Sensor mit:

- **Zustand:** Klartext-Status (z. B. „In Zustellung“, „Zugestellt“)
- **Attribute:** `tracking_id`, `carrier` (`dhl`/`dpd`), `group` (Phase), `direction`, `delivered`, `tracking_url`, `events` (kompletter Verlauf, neueste zuerst), bei DHL zusätzlich `delivery_window_from`/`_to`

Zusätzlich der **anbieterübergreifende** Sammel-Sensor **„Heute in Zustellung“** (`sensor.heute_in_zustellung`): Zustand ist die Gesamtzahl der Sendungen, die gerade im Zustellfahrzeug sind; Attribut `shipments` enthält die Liste (inkl. Anbieter).

## Oberfläche („Paketverfolgung“ in der Seitenleiste)

Nach der Einrichtung erscheint automatisch ein eigener Menüpunkt **Paketverfolgung**:

- **Übersicht:** Kacheln (Gesamt / Unterwegs / Heute in Zustellung / Zugestellt), Eingabefeld „Sendungsnummer hinzufügen (DHL oder DPD)“ und die Liste aller Sendungen – zuletzt geändert zuerst, mit Datum und Uhrzeit der letzten Änderung.
- **Detailseite:** Klick auf eine Sendung → aktueller Status, Eckdaten, Link zur Anbieter-Seite, Button „Jetzt aktualisieren“ und der **komplette Sendungsverlauf** als Zeitleiste.
- **Einstellungen:** Button am Ende der Übersicht öffnet die Integration – dort beim jeweiligen Eintrag über das Zahnrad PLZ und Sendungsnummern eingeben, oder per „Eintrag hinzufügen“ den DPD-Login.

Reine Weboberfläche ohne zusätzliche Abfragen – zeigt dieselben Daten wie die Sensoren, nur aufbereitet.

## Bekannte Einschränkungen

- Alle Schnittstellen sind inoffiziell und können bei anbieterseitigen Änderungen brechen. Bitte in diesem Fall ein Issue öffnen.
- DHL: keine automatische Kontoerkennung – Sendungsnummern müssen eingetragen werden.
- DPD: manche Sendungen sind ohne Empfänger-PLZ nicht öffentlich abrufbar; nur ein myDPD-Konto pro Eintrag.
