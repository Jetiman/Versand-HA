# Paketverfolgung für Home Assistant

Zeigt den Status deiner DHL-Sendungen als Sensoren in Home Assistant an. Du trägst die Sendungsnummern ein, die Integration fragt sie regelmäßig bei DHL ab.

Nutzt DHLs öffentliche Sendungsverfolgungs-Suche (dieselbe, die auch die Seite [dhl.de/sendungsverfolgung](https://www.dhl.de/de/privatkunden/dhl-sendungsverfolgung.html) verwendet) – **kein DHL-Login, keine Zugangsdaten, kein Konto nötig.**

> ⚠️ **Hinweis:** Es handelt sich um eine **inoffizielle** Schnittstelle (kein offiziell dokumentiertes API). DHL kann sie jederzeit ohne Vorwarnung ändern.

## Installation über HACS

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. Dieses Repository als URL eintragen, Kategorie **Integration** wählen, hinzufügen.
3. „Paketverfolgung“ in HACS suchen und installieren.
4. Home Assistant neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Paketverfolgung“**.
2. Eine oder mehrere Sendungsnummern eingeben (nach jeder Nummer Enter drücken). Der Schritt kann auch leer übersprungen werden.
3. Fertig – pro Sendungsnummer wird automatisch ein Sensor angelegt.

**Sendungsnummern später hinzufügen/entfernen:** Einstellungen → Geräte & Dienste → Paketverfolgung → Zahnrad-Symbol bei „Paketverfolgung“ → dort auch das Aktualisierungsintervall (Standard: alle 15 Minuten) anpassbar.

## Was wird angezeigt?

Pro Sendungsnummer ein Sensor mit:

- **Zustand:** Klartext-Status (z. B. „In Zustellung“, „Zugestellt“)
- **Attribute:** Tracking-ID, Fortschrittsstufe (0–5), Richtung (eingehend/ausgehend), Zustellzeitfenster (falls verfügbar), Link zur DHL-Sendungsverfolgung

Liefert DHL zu einer Sendungsnummer keine Daten mehr (z. B. weil sie sehr alt ist), verschwindet der zugehörige Sensor automatisch. Um eine Sendung nicht mehr zu verfolgen, die Sendungsnummer einfach aus den Optionen entfernen.

## Warum keine automatische Erkennung neuer Pakete?

Ursprünglich sollte die Integration sich wie die DHL-App mit dem eigenen DHL-Konto anmelden und alle Sendungen automatisch erkennen – ganz ohne manuelle Eingabe. Das ist technisch möglich (reverse-engineerter Login-Flow, analog zu [ioBroker.parcel](https://github.com/TA2k/ioBroker.parcel)), aber der dafür nötige Endpunkt liefert aktuell (Stand 2026-08) trotz gültigem Login keine Sendungen zurück – vermutlich nutzt die App inzwischen einen anderen, internen Endpunkt. Die Sendungsnummer-Suche funktioniert dagegen zuverlässig und sogar ganz ohne Login.

Der Login-basierte Ansatz ist in der Git-Historie dieses Repositories vollständig erhalten und kann bei Bedarf wieder aufgegriffen werden.

## Bekannte Einschränkungen

- Aktuell ist nur DHL als Anbieter implementiert.
- Sendungsnummern müssen manuell eingetragen werden, keine automatische Kontoerkennung.
- Da inoffiziell: kann bei DHL-seitigen Änderungen brechen. Bitte in diesem Fall ein Issue öffnen.
