# Paketverfolgung für Home Assistant

Zeigt den Status deiner DHL-Sendungen als Sensoren in Home Assistant an. Sendungen können entweder manuell per Sendungsnummer hinzugefügt oder nach einmaliger Anmeldung automatisch aus deinem DHL-Konto erkannt werden.

Die Integration nutzt inoffizielle DHL-Schnittstellen. Für manuell eingetragene Sendungsnummern ist kein DHL-Login erforderlich. Die optionale automatische Kontoerkennung verwendet einen Login-Flow analog zur DHL-App.

> ⚠️ **Hinweis:** Es handelt sich um **inoffizielle, nicht dokumentierte Schnittstellen**. DHL kann diese jederzeit ohne Vorwarnung ändern.

## Installation über HACS

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**.
2. Dieses Repository als URL eintragen, Kategorie **Integration** wählen, hinzufügen.
3. „Paketverfolgung“ in HACS suchen und installieren.
4. Home Assistant neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Paketverfolgung“**.
2. Optional eine oder mehrere Sendungsnummern eingeben (nach jeder Nummer Enter drücken).
3. Optional **„Sendungen automatisch aus meinem DHL-Konto erkennen“** aktivieren.
4. Ohne Kontoerkennung ist die Einrichtung damit abgeschlossen. Bei aktivierter Kontoerkennung dem DHL-Login-Dialog folgen.

**Sendungsnummern später hinzufügen/entfernen:** Einstellungen → Geräte & Dienste → Paketverfolgung → Zahnrad-Symbol bei „Paketverfolgung“. Dort können auch die automatische DHL-Kontoerkennung und das Aktualisierungsintervall angepasst werden.

## Automatische Erkennung über das DHL-Konto

Die Kontoerkennung ist optional. Home Assistant speichert nach erfolgreicher Anmeldung die DHL-Session und kann dadurch die im Konto vorhandenen Sendungen automatisch erkennen. Ein erneutes manuelles Eintragen der Sendungsnummern ist nicht erforderlich.

### DHL-Anmeldung und `dhllogin://`-Weiterleitung

DHL verwendet nach erfolgreicher Anmeldung eine benutzerdefinierte `dhllogin://`-Weiterleitungsadresse. Ein normaler Desktop-Browser kann diese Adresse nicht öffnen. Deshalb muss die vollständige Weiterleitungsadresse aus den Entwicklerwerkzeugen des Browsers kopiert und anschließend in Home Assistant eingefügt werden.

Empfohlen wird ein Desktop-Browser mit Entwicklerwerkzeugen, z. B. Chrome oder Edge:

1. Im Home-Assistant-Dialog **„Mit DHL verbinden“** den angezeigten **DHL Login** öffnen.
2. **Vor bzw. während der Anmeldung die Entwicklerwerkzeuge öffnen** (`F12` bzw. `Strg`+`Shift`+`I`) und den Tab **Network / Netzwerk** auswählen.
3. Bei DHL normal anmelden und die Anmeldung vollständig abschließen. Dass der Browser die abschließende DHL-App-Weiterleitung nicht öffnen kann, ist hierbei erwartbar.
4. Im Tab **Network / Netzwerk** im Filterfeld nach `login?code` suchen.
5. Den passenden Eintrag `login?code=...` anklicken.
6. Rechts **Headers / Header** öffnen und im Abschnitt **General / Allgemein** die vollständige **Request URL** kopieren. Sie beginnt mit:

   ```text
   dhllogin://de.deutschepost.dhl/login?code=...
   ```

7. Diese **vollständige Request URL** in das Feld `dhllogin:// Weiterleitungsadresse` in Home Assistant einfügen und mit **OK** bestätigen.
8. Nach erfolgreichem Token-Austausch ist das DHL-Konto verbunden. Die darin gefundenen Sendungen werden anschließend automatisch als Sensoren angelegt.

> **Wichtig:** Nicht die vorherige `https://login.dhl.de/...`-Adresse aus der Browser-Adresszeile kopieren. Benötigt wird ausdrücklich die `dhllogin://...login?code=...`-**Request URL** aus dem Netzwerk-Tab.

> **Sicherheit:** Die `dhllogin://...code=...`-URL enthält einen kurzfristig gültigen Autorisierungscode. Nicht öffentlich posten oder in Screenshots ungeschwärzt weitergeben.

## Was wird angezeigt?

Pro Sendungsnummer ein Sensor mit:

- **Zustand:** Klartext-Status (z. B. „In Zustellung“, „Zugestellt“)
- **Attribute:** Tracking-ID, Fortschrittsstufe (0–5), Richtung (eingehend/ausgehend), Zustellzeitfenster (falls verfügbar), Link zur DHL-Sendungsverfolgung

Liefert DHL zu einer Sendungsnummer keine Daten mehr (z. B. weil sie sehr alt ist), verschwindet der zugehörige Sensor automatisch. Bei manueller Verfolgung kann eine Sendung außerdem über die Optionen entfernt werden.

Zusätzlich gibt es einen Sammel-Sensor **„Heute in Zustellung“** (`sensor.heute_in_zustellung`): Zustand ist die Anzahl der Sendungen, die DHL aktuell im Zustellfahrzeug hat (Fortschrittsstufe 4 – das setzt DHL nur am Tag der tatsächlichen Zustellung), Attribut `shipments` enthält die Liste dieser Sendungen für eine Dashboard-Karte.

## Bekannte Einschränkungen

- Aktuell ist nur DHL als Anbieter implementiert.
- Die automatische Kontoerkennung basiert auf einem inoffiziellen, reverse-engineerten DHL-App-Login und kann durch Änderungen bei DHL brechen.
- Der `dhllogin://`-Callback muss derzeit manuell über die Entwicklerwerkzeuge des Browsers kopiert werden.
- Die öffentliche Sendungsverfolgung und die Kontoerkennung sind keine offiziell dokumentierten DHL-APIs.
