# Amazon: Position des Lieferwagens und Orte im Verlauf

**Entscheidung:** Position und Orte werden direkt in Versand-HA gelesen. Die Orte stehen auf der Sendungsseite, die ohnehin bei jeder Abfrage geladen wird. Die Position kostet eine zusätzliche Anfrage an Amazons Kartenadresse (`map-tracking-deans-proxy`), und zwar nur bei Amazon-eigenen Lieferwagen mit höchstens 10 verbleibenden Stopps. Das `csrfToken` dafür kommt von derselben Sendungsseite.

**Alternativen:** eine eigene Begleit-Integration (Vorschlag aus Issue #17), die die Amazon-Sitzung von Versand-HA mitnutzt und die Seite ein zweites Mal lädt.

**Grund:** Keine doppelten Seitenabrufe, kein Zugriff einer fremden Integration auf interne Teile von Versand-HA, Orte ohne nachträgliches Zuordnen über Zeit und Text. Die Position ändert sich minütlich: Sie ist vom HA-Verlauf ausgenommen (`_unrecorded_attributes`) und wird nicht ins Archiv verschwundener Sendungen geschrieben. Fehler bei der Kartenabfrage lassen die normale Abfrage unberührt.
