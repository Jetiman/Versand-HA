# Paketverfolgung für Home Assistant

Zeigt den Status deiner Paketsendungen als Sensoren in Home Assistant an. Unterstützt **DHL** (Sendungsnummern) und **DPD** (Konto-Login mit automatischer Erkennung aller Sendungen).

## Installation über HACS

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. Dieses Repository als URL eintragen, Kategorie **Integration** wählen, hinzufügen.
3. „Paketverfolgung“ in HACS suchen und installieren.
4. Home Assistant neu starten.

Für jeden Anbieter separat **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Paketverfolgung“** ausführen und den gewünschten Anbieter wählen. Beide können parallel eingerichtet sein.

## DHL

Nutzt DHLs öffentliche Sendungsverfolgungs-Suche (dieselbe, die auch die Seite [dhl.de/sendungsverfolgung](https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html) verwendet) – **kein DHL-Login, keine Zugangsdaten, kein Konto nötig.**

> ⚠️ **Hinweis:** Es handelt sich um eine **inoffizielle** Schnittstelle (kein offiziell dokumentiertes API). DHL kann sie jederzeit ohne Vorwarnung ändern.

### Einrichtung

1. Anbieter „DHL“ wählen.
2. Eine oder mehrere Sendungsnummern eingeben (nach jeder Nummer Enter drücken). Der Schritt kann auch leer übersprungen werden.
3. Fertig – pro Sendungsnummer wird automatisch ein Sensor angelegt.

**Sendungsnummern später hinzufügen/entfernen:** Einstellungen → Geräte & Dienste → Paketverfolgung → Zahnrad-Symbol beim DHL-Eintrag → dort auch das Aktualisierungsintervall (Standard: alle 15 Minuten) anpassbar.

Liefert DHL zu einer Sendungsnummer keine Daten mehr (z. B. weil sie sehr alt ist), verschwindet der zugehörige Sensor automatisch.

### Warum keine automatische Erkennung neuer DHL-Pakete?

Ursprünglich sollte die Integration sich wie die DHL-App mit dem eigenen DHL-Konto anmelden und alle Sendungen automatisch erkennen – ganz ohne manuelle Eingabe. Das ist technisch möglich (reverse-engineerter Login-Flow, analog zu [ioBroker.parcel](https://github.com/TA2k/ioBroker.parcel)), aber der dafür nötige Endpunkt liefert aktuell (Stand 2026-08) trotz gültigem Login keine Sendungen zurück – vermutlich nutzt die App inzwischen einen anderen, internen Endpunkt. Die Sendungsnummer-Suche funktioniert dagegen zuverlässig und sogar ganz ohne Login. Der Login-basierte Ansatz ist in der Git-Historie dieses Repositories vollständig erhalten und kann bei Bedarf wieder aufgegriffen werden.

## DPD

Meldet sich mit deinem **myDPD-Konto** an (SOAP-API der offiziellen DPD-App "Paketnavigator") und zeigt automatisch **alle** Sendungen (gesendet, empfangen, Retouren) an – keine manuelle Eingabe von Sendungsnummern nötig.

> ⚠️ **Hinweis:** Ebenfalls eine **inoffizielle** Schnittstelle (Partner-Zugangsdaten aus der Android-App extrahiert). Kann bei DPD-seitigen Änderungen brechen.

### Einrichtung

1. Anbieter „DPD“ wählen.
2. Mit Benutzername/E-Mail und Passwort deines myDPD-Kontos anmelden.
3. Fertig – alle Sendungen werden automatisch als Sensoren angelegt und bei jeder Aktualisierung neu abgeglichen.

Das Passwort wird nur lokal in Home Assistant gespeichert (wie bei jeder anderen Cloud-Integration).

## Was wird angezeigt?

Pro Sendung (DHL) bzw. Paket (DPD) ein Sensor mit:

- **Zustand:** Klartext-Status (z. B. „In Zustellung“, „Zugestellt“)
- **Attribute:** Tracking-ID, Status/Fortschritt, Richtung, Link zur Sendungsverfolgung (bei DHL zusätzlich Zustellzeitfenster und komplette Verlaufshistorie)

Zusätzlich ein **anbieterübergreifender** Sammel-Sensor **„Heute in Zustellung“** (`sensor.heute_in_zustellung`): Zustand ist die Gesamtzahl der Sendungen (DHL + DPD zusammen), die gerade im Zustellfahrzeug sind, Attribut `shipments` enthält die Liste dieser Sendungen (inkl. Anbieter) für eine Dashboard-Karte.

## Bekannte Einschränkungen

- Beide Schnittstellen sind inoffiziell und können bei anbieterseitigen Änderungen brechen. Bitte in diesem Fall ein Issue öffnen.
- DHL: Sendungsnummern müssen manuell eingetragen werden, keine automatische Kontoerkennung (siehe oben).
- DPD: nur ein myDPD-Konto pro eingerichtetem Eintrag; mehrere Konten können durch mehrfaches Hinzufügen der Integration verfolgt werden.
