# Paketverfolgung für Home Assistant

Zeigt den Status deiner Paketsendungen als Sensoren in Home Assistant an. **Aktuell wird DHL unterstützt**, weitere Paketdienste (z. B. GLS, Hermes) sollen als eigene Anbieter innerhalb dieser Integration folgen.

Für DHL: automatisch, ohne dass du Trackingnummern manuell eintragen musst – genau wie die DHL-App zeigt sie alle Sendungen an, die in deinem DHL-Konto hinterlegt sind. Dafür nutzt sie den gleichen (inoffiziellen) Login- und Abfrage-Mechanismus wie die DHL Paket App. Die Herangehensweise orientiert sich an [ioBroker.parcel](https://github.com/TA2k/ioBroker.parcel).

> ⚠️ **Hinweis:** Es handelt sich um eine **inoffizielle** Schnittstelle. DHL kann sie jederzeit ohne Vorwarnung ändern oder abschalten. Es werden keine Zugangsdaten (Benutzername/Passwort) gespeichert, sondern nur ein Login-Token, das Home Assistant regelmäßig automatisch erneuert.

## Installation über HACS

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. Dieses Repository als URL eintragen, Kategorie **Integration** wählen, hinzufügen.
3. „Paketverfolgung“ in HACS suchen und installieren.
4. Home Assistant neu starten.

## Einrichtung (DHL)

Der einzige etwas ungewöhnliche Schritt ist der einmalige Login, da DHL keinen normalen Redirect für Drittanbieter-Apps erlaubt. **Wichtig:** Die Ziel-URL taucht danach *nicht* in der Adressleiste auf, sondern nur in der Entwickler-Konsole des Browsers – bitte genau der Reihenfolge folgen:

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Paketverfolgung“**.
2. Der Dialog zeigt dir einen Login-Link. Öffne ihn **in Chrome oder Edge** (Firefox/Safari zeigen die Meldung aus Schritt 4 nicht auf die gleiche Weise) und melde dich mit deinem DHL-Konto (den gleichen Zugangsdaten wie in der DHL-App) an.
3. Öffne **vor oder direkt nach** dem Login die Entwicklertools (Taste **F12**, oder Rechtsklick auf die Seite → „Untersuchen“) und wechsle zum Tab **„Konsole“ / „Console“**.
4. Nach erfolgreichem Login versucht die Seite automatisch auf `dhllogin://...` weiterzuleiten. Das schlägt fehl – die Adressleiste bleibt unverändert (ggf. auf einer leeren/schwarzen Seite), **aber in der Konsole erscheint eine rote Meldung**: `Failed to launch 'dhllogin://de.deutschepost.dhl/login?code=...' because the scheme does not have a registered handler.`
5. **Rechtsklick auf den blauen `dhllogin://...`-Link in dieser Meldung → „Link-Adresse kopieren“** (Copy link address). *Nicht* die URL aus der Adressleiste kopieren – die enthält keinen Code und führt zu „Anmeldung fehlgeschlagen“.
6. Die kopierte URL in das Textfeld im Home-Assistant-Dialog einfügen und bestätigen.

Falls in der Konsole nichts erscheint: Seite neu laden reicht meist nicht – dann den Login-Link erneut öffnen und den Ablauf mit bereits offener Konsole wiederholen.

Danach werden automatisch Sensor-Entities für alle aktiven Sendungen deines Kontos angelegt und regelmäßig aktualisiert (Standard: alle 15 Minuten, im Optionen-Dialog der Integration änderbar).

Läuft die Anmeldung irgendwann ab, fordert Home Assistant dich automatisch über eine Benachrichtigung zur erneuten Anmeldung (gleicher Ablauf) auf.

## Was wird angezeigt?

Pro aktiver Sendung ein Sensor mit:

- **Zustand:** Klartext-Status (z. B. „In Zustellung“, „Zugestellt“)
- **Attribute:** Tracking-ID, Fortschrittsstufe (0–5), Richtung (eingehend/ausgehend), Zustellzeitfenster (falls verfügbar), Link zur DHL-Sendungsverfolgung

Zugestellte bzw. archivierte Sendungen verschwinden aus der DHL-App-Übersicht nach einiger Zeit – die zugehörigen Sensoren werden dann automatisch entfernt.

## Bekannte Einschränkungen

- Aktuell ist nur DHL als Anbieter implementiert. Weitere Paketdienste (GLS, Hermes, ...) sind als spätere Erweiterung angedacht, aber noch nicht umgesetzt.
- Der Login-Schritt ist manuell (Copy & Paste), da DHL das `dhllogin://`-Schema nur für die native App registriert.
- Da inoffiziell: kann bei DHL-seitigen Änderungen brechen. Bitte in diesem Fall ein Issue öffnen.
