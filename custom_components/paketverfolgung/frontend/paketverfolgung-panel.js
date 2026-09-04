/*
 * Paketverfolgung panel - a custom Home Assistant sidebar panel.
 *
 * Buildless, dependency-free (plain custom element, no Lit bundle) so it can
 * ship straight from the integration folder without a frontend toolchain.
 *
 * Two views, switched on the panel route:
 *   /paketverfolgung              -> list of all tracked shipments
 *   /paketverfolgung/<entity_id>  -> one shipment with its full history
 *
 * All data comes from the sensor entities the integration already creates
 * (one per shipment/parcel, identified by the `tracking_id` attribute).
 */

const PROVIDER_LABELS = { dhl: "DHL", dpd: "DPD", hermes: "Hermes", amazon: "Amazon" };

// The integration's brand icon (brand/icon.svg), inlined so the panel needs
// no extra static asset.
const LOGO_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" aria-hidden="true">
  <polygon points="128,40 216,78 128,116 40,78" fill="#E3A96A"/>
  <polygon points="40,78 128,116 128,208 40,170" fill="#B97A3D"/>
  <polygon points="216,78 128,116 128,208 216,170" fill="#C9884B"/>
  <polygon points="128,40 152,50.5 128,61 104,50.5" fill="#F3D9B1"/>
  <polygon points="118,44.4 138,53 138,103 118,94.4" fill="#F3D9B1"/>
  <polygon points="118,94.4 128,98.8 128,204.8 118,200.4" fill="#8A5A2B" opacity="0.55"/>
  <polygon points="128,98.8 138,103 138,209 128,204.8" fill="#8A5A2B" opacity="0.35"/>
  <polygon points="40,116 128,153.4 128,164.6 40,127.2" fill="#F3D9B1" opacity="0.9"/>
  <polygon points="128,153.4 216,116 216,127.2 128,164.6" fill="#F3D9B1" opacity="0.9"/>
  <polygon points="128,40 216,78 216,170 128,208 40,170 40,78" fill="none" stroke="#6E4423" stroke-width="4" stroke-linejoin="round"/>
  <polyline points="40,78 128,116 216,78" fill="none" stroke="#6E4423" stroke-width="4" stroke-linejoin="round"/>
  <line x1="128" y1="116" x2="128" y2="208" stroke="#6E4423" stroke-width="4"/>
  <g transform="translate(168,150)">
    <circle cx="34" cy="34" r="40" fill="#2F6FED"/>
    <circle cx="34" cy="34" r="40" fill="none" stroke="#F5F8FF" stroke-width="5"/>
    <path d="M34 14 C22 14 13 23 13 35 C13 50 34 62 34 62 C34 62 55 50 55 35 C55 23 46 14 34 14 Z" fill="#F5F8FF"/>
    <circle cx="34" cy="34" r="8" fill="#2F6FED"/>
  </g>
</svg>`;

class PaketverfolgungPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._route = null;
    this._narrow = false;
    this._sig = null;
    this._draftTracking = "";
    this._addBusy = false;
    this._addResult = null;
    this._onClick = this._onClick.bind(this);
    this._onInput = this._onInput.bind(this);
    this._onSubmit = this._onSubmit.bind(this);
    this._onChange = this._onChange.bind(this);
    this._onPopState = () => this._render();
    this._nextUpdate = null;
    this._tick = null;
    this._archiveOpen = false;
    this._settingsOpen = false;
    this._notifyBusy = false;
    this._notifyPickerOpen = false;
    this._notifyFilter = "";
  }

  connectedCallback() {
    window.addEventListener("popstate", this._onPopState);
    this.addEventListener("click", this._onClick);
    this.addEventListener("input", this._onInput);
    this.addEventListener("submit", this._onSubmit);
    this.addEventListener("change", this._onChange);
    this._tick = setInterval(() => this._tickCountdown(), 15000);
    this._render(true);
  }

  disconnectedCallback() {
    window.removeEventListener("popstate", this._onPopState);
    if (this._tick) clearInterval(this._tick);
  }

  _tickCountdown() {
    const el = this.querySelector(".pv-next-value");
    if (el) el.textContent = fmtCountdown(this._nextUpdate);
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  set route(route) {
    this._route = route;
    this._render();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menuBtn = this.querySelector("ha-menu-button");
    if (menuBtn) menuBtn.narrow = narrow;
  }

  set panel(_panel) {
    /* config unused */
  }

  /* ---------- data ---------- */

  _combinedSensor() {
    // The "Heute in Zustellung" summary sensor - found by its `shipments`
    // attribute rather than a fixed entity_id (which can be suffixed
    // _2/_3 after registry churn from older versions).
    if (!this._hass) return null;
    for (const s of Object.values(this._hass.states)) {
      if (
        s.entity_id.startsWith("sensor.") &&
        s.attributes &&
        Array.isArray(s.attributes.shipments)
      ) {
        return s;
      }
    }
    return null;
  }

  _shipments() {
    if (!this._hass) return [];
    const out = [];
    for (const stateObj of Object.values(this._hass.states)) {
      if (!stateObj.entity_id.startsWith("sensor.")) continue;
      const a = stateObj.attributes || {};
      if (!a.tracking_id) continue;
      const url = a.tracking_url || "";
      const carrier =
        (a.carrier ||
          (url.includes("dpd")
            ? "dpd"
            : url.includes("hermes")
            ? "hermes"
            : "dhl")) + "";
      const group = a.group || "";
      const delivered = a.delivered === true || group === "delivered";
      const events = Array.isArray(a.events) ? a.events : [];
      // "last change" of the shipment itself: newest carrier event if we
      // have one; for a delivered parcel without dated events, the
      // computed delivery moment; otherwise when the sensor last changed.
      const changed =
        (events[0] && events[0].datum) ||
        a.delivered_at ||
        stateObj.last_changed ||
        null;
      out.push({
        entity_id: stateObj.entity_id,
        name: a.friendly_name || stateObj.entity_id,
        status: stateObj.state,
        icon: a.icon || "mdi:package-variant-closed",
        tracking_id: String(a.tracking_id),
        provider: PROVIDER_LABELS[carrier] || "?",
        carrier,
        delivery_carrier: a.delivery_carrier || null,
        carrier_tracking_id: a.carrier_tracking_id || null,
        group,
        direction: a.direction || null,
        forced_direction: a.forced_direction || null,
        delivery_from: a.delivery_window_from || null,
        delivery_to: a.delivery_window_to || null,
        tracking_url: url || null,
        events,
        delivered,
        archived: a.archived === true,
        delivered_at: a.delivered_at || null,
        protected: a.protected === true,
        removable: a.removable === true,
        forced: a.forced_carrier || null,
        custom_name: a.custom_name || null,
        out_for_delivery: group === "out_for_delivery" && !delivered,
        changed,
        last_updated: stateObj.last_updated,
        last_changed: stateObj.last_changed,
      });
    }
    out.sort((x, y) => {
      const tx = x.changed ? Date.parse(x.changed) : 0;
      const ty = y.changed ? Date.parse(y.changed) : 0;
      return (ty || 0) - (tx || 0);
    });
    return out;
  }

  _currentEntityId() {
    let path = (this._route && this._route.path) || "";
    if (!path) {
      const p = window.location.pathname.split("/paketverfolgung")[1] || "";
      path = p;
    }
    return path.replace(/^\/+/, "").split("/")[0] || null;
  }

  _buildInfo() {
    const a = (this._combinedSensor() || {}).attributes || {};
    const ver = a.integration_version || "";
    const commit = a.integration_commit || "";
    if (!ver) return "";
    const tail = commit && commit !== "0000000" ? " · " + commit : "";
    return `<div class="pv-version">Paketverfolgung ${esc(ver + tail)}</div>`;
  }

  _notifyConfig() {
    const a = (this._combinedSensor() || {}).attributes || {};
    return {
      enabled: a.notify_enabled === true,
      targets: Array.isArray(a.notify_targets) ? a.notify_targets : [],
      services: Array.isArray(a.notify_services) ? a.notify_services : [],
      ofdOnly: a.notify_out_for_delivery_only === true,
      shortName: a.notify_short_name === true,
    };
  }

  _notifyPickerHtml(nc) {
    const all = [...new Set([...nc.services, ...nc.targets])].sort();
    const chosen = nc.targets.slice().sort();
    const summary = chosen.length
      ? chosen.map((t) => "notify." + t).join(", ")
      : "keine ausgewählt";
    if (!all.length) {
      return `<div class="pv-set-hint">Keine <code>notify.*</code>-Dienste gefunden –
        z. B. die Home-Assistant-App auf dem Handy einrichten.</div>`;
    }
    const rows = all
      .map(
        (svc) => `<label class="pv-check" data-svc="${esc(svc)}">
          <input type="checkbox" data-notify-target="${esc(svc)}"
            ${nc.targets.includes(svc) ? "checked" : ""}
            ${this._notifyBusy ? "disabled" : ""} />
          <span>notify.${esc(svc)}${
          nc.services.includes(svc) ? "" : " (nicht gefunden)"
        }</span>
        </label>`
      )
      .join("");
    return `
      <details class="pv-picker"${this._notifyPickerOpen ? " open" : ""}>
        <summary data-picker-toggle>
          <span class="pv-picker-label">Ziele</span>
          <span class="pv-picker-value">${esc(summary)}</span>
        </summary>
        <div class="pv-picker-body">
          <input type="text" class="pv-picker-filter" data-notify-filter
            placeholder="filtern …" value="${esc(this._notifyFilter)}" />
          <div class="pv-notify-list">${rows}</div>
        </div>
      </details>`;
  }

  _applyNotifyFilter() {
    const q = (this._notifyFilter || "").trim().toLowerCase();
    this.querySelectorAll(".pv-notify-list [data-svc]").forEach((el) => {
      el.hidden = q ? !el.getAttribute("data-svc").toLowerCase().includes(q) : false;
    });
  }

  _setNotifications(enabled, targets, ofdOnly, shortName) {
    this._notifyBusy = true;
    this._sig = null;
    this._render(true);
    Promise.resolve(
      this._hass.callService("paketverfolgung", "set_notifications", {
        enabled,
        targets,
        out_for_delivery_only: ofdOnly === true,
        short_name: shortName === true,
      })
    ).finally(() => {
      this._notifyBusy = false;
      this._sig = null;
      this._render(true);
    });
  }

  _testNotification() {
    this._testBusy = true;
    this._testMsg = "";
    this._sig = null;
    this._render(true);
    Promise.resolve(
      this._hass.callService("paketverfolgung", "test_notification", {})
    )
      .then(() => {
        this._testMsg = "Test gesendet ✓";
      })
      .catch((e) => {
        this._testMsg = "Fehler: " + ((e && e.message) || e);
      })
      .finally(() => {
        this._testBusy = false;
        this._sig = null;
        this._render(true);
        clearTimeout(this._testTimer);
        this._testTimer = setTimeout(() => {
          this._testMsg = "";
          this._sig = null;
          this._render(true);
        }, 5000);
      });
  }

  /* ---------- rendering ---------- */

  _render(force) {
    if (!this._hass) return;
    const shipments = this._shipments();
    const entityId = this._currentEntityId();
    const combined = this._combinedSensor();
    const nextIso = combined && combined.attributes && combined.attributes.next_update;
    this._nextUpdate = nextIso ? new Date(nextIso) : null;
    const nc = this._notifyConfig();
    const sig = JSON.stringify({
      entityId,
      busy: this._addBusy,
      result: this._addResult,
      nextIso: nextIso || null,
      archiveOpen: this._archiveOpen,
      settingsOpen: this._settingsOpen,
      notifyBusy: this._notifyBusy,
      notifyPickerOpen: this._notifyPickerOpen,
      notify: nc,
      testBusy: this._testBusy || false,
      testMsg: this._testMsg || "",
      build: this._buildInfo(),
      items: shipments.map((s) => [
        s.entity_id,
        s.name,
        s.status,
        s.group,
        s.carrier,
        s.delivery_carrier,
        s.carrier_tracking_id,
        s.forced,
        s.changed,
        s.events.length,
        s.archived,
      ]),
    });
    if (!force && sig === this._sig) return;
    this._sig = sig;

    const body = entityId
      ? this._detailHtml(shipments.find((s) => s.entity_id === entityId), entityId)
      : this._listHtml(shipments);

    this.innerHTML = `
      <style>${STYLES}</style>
      <div class="pv-toolbar"><ha-menu-button></ha-menu-button></div>
      <div class="pv-content">${body}</div>
    `;

    const menuBtn = this.querySelector("ha-menu-button");
    if (menuBtn) {
      menuBtn.hass = this._hass;
      menuBtn.narrow = this._narrow;
    }
    const input = this.querySelector('input[name="tracking"]');
    if (input && this._draftTracking) input.value = this._draftTracking;
    if (this._notifyFilter) this._applyNotifyFilter();
  }

  _listHtml(allShipments) {
    const nc = this._notifyConfig();
    const shipments = allShipments.filter((s) => !s.archived);
    const archived = allShipments.filter((s) => s.archived);
    const total = shipments.length;
    const delivered = shipments.filter((s) => s.delivered).length;
    const inReview = shipments.filter((s) => s.group === "unknown").length;
    // "Unterwegs" = actually moving (in transit or out for delivery) - not
    // just "active and not delivered", so a registered/announced label or a
    // still-to-be-shipped Amazon order isn't counted.
    const inTransit = shipments.filter(
      (s) =>
        !s.delivered &&
        (s.group === "transit" || s.group === "out_for_delivery")
    ).length;
    let outForDelivery = shipments.filter((s) => s.out_for_delivery && !s.delivered)
      .length;
    const combined = this._combinedSensor();
    if (combined && !Number.isNaN(Number(combined.state))) {
      outForDelivery = Number(combined.state);
    }

    const stats = [
      ["Sendungen", total],
      ["Unterwegs", inTransit],
      ["In Zustellung", outForDelivery],
    ]
      .map(
        ([label, value]) => `
        <div class="pv-stat">
          <div class="pv-stat-value">${value}</div>
          <div class="pv-stat-label">${esc(label)}</div>
        </div>`
      )
      .join("");

    const canAdd = Boolean(
      this._hass.services.paketverfolgung &&
        this._hass.services.paketverfolgung.add_tracking_number
    );

    const rows = shipments.length
      ? shipments.map((s, i) => this._rowHtml(s, i + 1)).join("")
      : archived.length
      ? `<div class="pv-empty">Keine aktiven Sendungen – alle sind im Archiv.</div>`
      : `<div class="pv-empty">Noch keine Sendungen. Füge unten eine Sendungsnummer hinzu,
          verbinde ein DPD- oder DHL-Konto und die Sendungen erscheinen automatisch.</div>`;

    const archiveBlock = archived.length
      ? `<details class="pv-archive"${this._archiveOpen ? " open" : ""}>
          <summary data-archive-toggle>Archiv (${archived.length}) · zugestellt vor über 24 h</summary>
          <div class="pv-list pv-archive-list">
            ${archived.map((s, i) => this._rowHtml(s, i + 1)).join("")}
          </div>
        </details>`
      : "";

    let resultMsg = "";
    if (this._addResult) {
      resultMsg = `<div class="pv-note ${this._addResult.ok ? "ok" : "err"}">${esc(
        this._addResult.text
      )}</div>`;
    }

    return `
      <div class="pv-header">
        <span class="pv-logo">${LOGO_SVG}</span>
        <div class="pv-brand">Paketverfolgung</div>
      </div>
      <div class="pv-stats">${stats}</div>
      <button class="pv-next" data-refresh-all>
        <ha-icon icon="mdi:timer-sync-outline"></ha-icon>
        <span>Nächste Aktualisierung <b class="pv-next-value">${esc(
          fmtCountdown(this._nextUpdate)
        )}</b></span>
        <span class="pv-next-now">jetzt aktualisieren</span>
      </button>
      ${
        canAdd
          ? `<form class="pv-add" autocomplete="off">
              <input name="tracking" type="text" inputmode="numeric"
                placeholder="Sendungsnummer hinzufügen (DHL, DPD, Hermes)" />
              <button type="submit" ${this._addBusy ? "disabled" : ""}>
                ${this._addBusy ? "…" : "Hinzufügen"}
              </button>
            </form>
            ${resultMsg}`
          : ""
      }
      ${
        inReview
          ? `<div class="pv-note">${inReview} Sendung${
              inReview === 1 ? "" : "en"
            } in Prüfung – noch keinem Anbieter zugeordnet. Bleibt in der Liste, bis gelöscht.</div>`
          : ""
      }
      <div class="pv-list">${rows}</div>
      ${archiveBlock}

      <details class="pv-settings-box"${this._settingsOpen ? " open" : ""}>
        <summary data-settings-toggle>Einstellungen</summary>
        <div class="pv-settings-body">
        <button class="pv-settings" data-nav="/config/integrations/integration/paketverfolgung">
          <ha-icon icon="mdi:cog-outline"></ha-icon>
          Integration öffnen (Konten, PLZ, Intervall …)
        </button>
        <div class="pv-footer-hint pv-hint-tight">
          Zeigt beim Eintrag „Sendungsnummern“ über das Zahnrad alle Optionen
          (u. a. die DHL-Konto-Anmeldung). Ein DPD- oder Amazon-Konto fügst du
          dort über „Eintrag hinzufügen“ hinzu.
        </div>
        <div class="pv-set-title">Benachrichtigungen</div>
        <label class="pv-switch">
          <input type="checkbox" data-notify-toggle ${nc.enabled ? "checked" : ""} ${
      this._notifyBusy ? "disabled" : ""
    } />
          <span>Bei neuer Sendung oder Statusänderung benachrichtigen</span>
        </label>
        ${
          nc.enabled
            ? `<label class="pv-switch pv-switch-sub">
                 <input type="checkbox" data-notify-ofd ${
                   nc.ofdOnly ? "checked" : ""
                 } ${this._notifyBusy ? "disabled" : ""} />
                 <span>Nur wenn eine Sendung in Zustellung geht <em>(Preview)</em></span>
               </label>
               <label class="pv-switch pv-switch-sub">
                 <input type="checkbox" data-notify-short ${
                   nc.shortName ? "checked" : ""
                 } ${this._notifyBusy ? "disabled" : ""} />
                 <span>Langen Sendungsnamen kürzen</span>
               </label>
               <div class="pv-test-row">
                 <button class="pv-mini-btn" data-notify-test ${
                   this._testBusy ? "disabled" : ""
                 }>Test senden</button>
                 ${
                   this._testMsg
                     ? `<span class="pv-test-msg">${esc(this._testMsg)}</span>`
                     : ""
                 }
               </div>`
            : ""
        }
        ${nc.enabled ? this._notifyPickerHtml(nc) : ""}
        ${this._buildInfo()}
        </div>
      </details>
    `;
  }

  _rowHtml(s, pos) {
    const generic =
      !s.name ||
      s.name === s.tracking_id ||
      Object.values(PROVIDER_LABELS).some(
        (p) => s.name === `${p} ${s.tracking_id}`
      );
    const prov = s.provider === "?" ? "" : s.provider;
    const dim = [prov, s.changed ? fmtShort(s.changed) : ""]
      .filter(Boolean)
      .map(esc)
      .join(" · ");
    return `
      <button class="pv-row" data-entity="${esc(s.entity_id)}">
        <div class="pv-row-num">${pos}</div>
        <ha-icon class="pv-row-icon" icon="${esc(s.icon)}"></ha-icon>
        <div class="pv-row-main${generic ? "" : " titled"}">
          ${generic ? "" : `<div class="pv-row-name">${esc(s.name)}</div>`}
          <div class="pv-row-id">${esc(s.tracking_id)}</div>
          <div class="pv-row-meta">
            <span class="pv-row-status ${s.delivered ? "done" : ""}">${esc(s.status)}</span>
            ${dim ? `<span class="pv-row-dim">${dim}</span>` : ""}
          </div>
        </div>
        <ha-icon class="pv-chevron" icon="mdi:chevron-right"></ha-icon>
      </button>
    `;
  }

  _detailHtml(s, entityId) {
    if (!s) {
      return `
        <a class="pv-back" data-back>← Übersicht</a>
        <div class="pv-empty">Diese Sendung ist nicht mehr verfügbar.</div>`;
    }

    const meta = [
      ["Anbieter", s.provider === "?" ? "in Prüfung" : s.provider],
      ["Zusteller", s.delivery_carrier],
      [s.carrier === "amazon" ? "Bestellnummer" : "Sendungsnummer", s.tracking_id],
      [
        "Trackingnummer",
        s.carrier_tracking_id && s.carrier_tracking_id !== s.tracking_id
          ? s.carrier_tracking_id
          : null,
      ],
      [
        "Zustellzeitfenster",
        // DHL sometimes gives only a date (no time) - that renders as
        // "02:00 – 02:00"; skip a window with no real span.
        s.delivery_from &&
        s.delivery_to &&
        fmtTime(s.delivery_from) !== fmtTime(s.delivery_to)
          ? `${fmtTime(s.delivery_from)} – ${fmtTime(s.delivery_to)}`
          : null,
      ],
    ]
      .filter(([, v]) => v)
      .map(
        ([k, v]) => `
        <div class="pv-meta-item">
          <div class="pv-meta-key">${esc(k)}</div>
          <div class="pv-meta-val">${esc(v)}</div>
        </div>`
      )
      .join("");

    let timeline = "";
    if (s.events.length) {
      timeline = s.events
        .map(
          (ev, i) => `
        <li class="pv-ev ${i === 0 ? "current" : ""}">
          <div class="pv-ev-dot"></div>
          <div class="pv-ev-body">
            <div class="pv-ev-status">${esc(ev.status || "")}</div>
            <div class="pv-ev-date">${esc(fmtDateTime(ev.datum))}</div>
          </div>
        </li>`
        )
        .join("");
    } else if (s.provider !== "?") {
      timeline = `
        <li class="pv-ev current">
          <div class="pv-ev-dot"></div>
          <div class="pv-ev-body">
            <div class="pv-ev-status">${esc(s.status)}</div>
            <div class="pv-ev-date">${esc(fmtRelative(s.last_changed))}</div>
          </div>
        </li>`;
    }

    let note = "";
    if (s.protected) {
      note = `<div class="pv-note">Diese DPD-Sendung ist geschützt. Hinterlege deine PLZ
        in den Optionen der Integration, damit der Verlauf abgerufen werden kann.</div>`;
    } else if (s.provider === "?") {
      note = `<div class="pv-note">Diese Sendung wird noch geprüft – kein Anbieter (DHL, DPD, Hermes, Amazon)
        hat bisher Daten dazu geliefert. Sie bleibt in der Liste und wird bei jeder Aktualisierung
        erneut geprüft, bis du sie löschst oder oben den Anbieter manuell festlegst.</div>`;
    } else if (s.archived) {
      note = `<div class="pv-note">Archiviert – vor über 24 Stunden zugestellt. Diese Sendung
        wird nicht mehr abgefragt, bleibt aber im Archiv abrufbar.</div>`;
    } else if (!s.events.length && (s.provider === "DPD" || s.provider === "Hermes" || s.provider === "Amazon")) {
      note = `<div class="pv-note">Für diese Sendung liegt noch kein Verlauf vor.</div>`;
    }

    return `
      <a class="pv-back" data-back>← Übersicht</a>

      <div class="pv-detail-head">
        <ha-icon class="pv-detail-icon" icon="${esc(s.icon)}"></ha-icon>
        <div>
          <div class="pv-detail-name">${esc(s.name)}</div>
          <div class="pv-detail-status ${s.delivered ? "done" : ""}">${esc(s.status)}</div>
          <div class="pv-detail-updated">Aktualisiert ${esc(fmtRelative(s.last_updated))}</div>
        </div>
      </div>

      <div class="pv-actions">
        <button data-refresh="${esc(entityId)}">Jetzt aktualisieren</button>
        ${
          s.tracking_url
            ? `<a class="pv-linkbtn" href="${esc(s.tracking_url)}" target="_blank"
                rel="noreferrer noopener">Beim Anbieter öffnen</a>`
            : ""
        }
        ${
          s.removable
            ? `<button class="pv-delete" data-delete="${esc(s.tracking_id)}">Löschen</button>`
            : ""
        }
      </div>

      <label class="pv-rename">
        <span>Name:</span>
        <input type="text" name="rename" data-name="${esc(s.tracking_id)}"
          value="${esc(s.custom_name || "")}" placeholder="${esc(s.name)}"
          autocomplete="off" />
      </label>

      ${
        s.removable
          ? `<label class="pv-carrier">
              <span>Anbieter${s.forced ? " (manuell gesetzt)" : ""}:</span>
              <select data-carrier="${esc(s.tracking_id)}">
                ${["auto", "dhl", "dpd", "hermes"]
                  .map((c) => {
                    const cur = s.forced || "auto";
                    const label =
                      c === "auto"
                        ? `automatisch${
                            !s.forced && s.provider !== "?"
                              ? " (" + s.provider + ")"
                              : ""
                          }`
                        : PROVIDER_LABELS[c];
                    return `<option value="${c}"${
                      c === cur ? " selected" : ""
                    }>${esc(label)}</option>`;
                  })
                  .join("")}
              </select>
            </label>`
          : ""
      }

      ${
        s.provider !== "?"
          ? `<label class="pv-carrier">
              <span>Richtung${s.forced_direction ? " (manuell gesetzt)" : ""}:</span>
              <select data-direction="${esc(s.tracking_id)}">
                ${["auto", "receive", "send"]
                  .map((d) => {
                    const cur = s.forced_direction || "auto";
                    const label =
                      d === "auto"
                        ? `automatisch${
                            !s.forced_direction && s.direction
                              ? " (" + directionLabel(s.direction) + ")"
                              : ""
                          }`
                        : directionLabel(d);
                    return `<option value="${d}"${
                      d === cur ? " selected" : ""
                    }>${esc(label)}</option>`;
                  })
                  .join("")}
              </select>
            </label>`
          : ""
      }

      <div class="pv-meta">${meta}</div>

      ${note}
      ${
        timeline
          ? `<div class="pv-section-title">Sendungsverlauf</div>
             <ul class="pv-timeline">${timeline}</ul>`
          : ""
      }
    `;
  }

  /* ---------- events ---------- */

  _onClick(ev) {
    const row = ev.target.closest("[data-entity]");
    if (row) {
      this._navigate(`/paketverfolgung/${row.getAttribute("data-entity")}`);
      return;
    }
    if (ev.target.closest("[data-back]")) {
      ev.preventDefault();
      this._navigate("/paketverfolgung");
      return;
    }
    const nav = ev.target.closest("[data-nav]");
    if (nav) {
      this._navigate(nav.getAttribute("data-nav"));
      return;
    }
    if (ev.target.closest("[data-archive-toggle]")) {
      ev.preventDefault();
      this._archiveOpen = !this._archiveOpen;
      this._sig = null;
      this._render(true);
      return;
    }
    if (ev.target.closest("[data-settings-toggle]")) {
      ev.preventDefault();
      this._settingsOpen = !this._settingsOpen;
      this._sig = null;
      this._render(true);
      return;
    }
    if (ev.target.closest("[data-picker-toggle]")) {
      ev.preventDefault();
      this._notifyPickerOpen = !this._notifyPickerOpen;
      this._sig = null;
      this._render(true);
      return;
    }
    if (ev.target.closest("[data-notify-test]")) {
      ev.preventDefault();
      if (!this._testBusy) this._testNotification();
      return;
    }
    const refreshAll = ev.target.closest("[data-refresh-all]");
    if (refreshAll) {
      const ids = this._shipments().map((s) => s.entity_id);
      if (ids.length) {
        this._hass.callService("homeassistant", "update_entity", {
          entity_id: ids,
        });
      }
      refreshAll.classList.add("busy");
      const v = refreshAll.querySelector(".pv-next-value");
      if (v) v.textContent = "wird aktualisiert …";
      return;
    }
    const refresh = ev.target.closest("[data-refresh]");
    if (refresh) {
      this._hass.callService("homeassistant", "update_entity", {
        entity_id: refresh.getAttribute("data-refresh"),
      });
      refresh.disabled = true;
      refresh.textContent = "Wird aktualisiert …";
      return;
    }
    const del = ev.target.closest("[data-delete]");
    if (del) {
      const num = del.getAttribute("data-delete");
      if (window.confirm(`Sendung ${num} aus der Liste entfernen?`)) {
        this._hass.callService("paketverfolgung", "remove_tracking_number", {
          tracking_number: num,
        });
        this._navigate("/paketverfolgung");
      }
    }
  }

  _onInput(ev) {
    if (ev.target.name === "tracking") {
      this._draftTracking = ev.target.value;
      this._addResult = null;
      return;
    }
    if (ev.target.matches("[data-notify-filter]")) {
      this._notifyFilter = ev.target.value;
      this._applyNotifyFilter();
    }
  }

  _onChange(ev) {
    if (ev.target.matches("[data-notify-toggle]")) {
      const nc = this._notifyConfig();
      this._setNotifications(
        ev.target.checked,
        nc.targets,
        nc.ofdOnly,
        nc.shortName
      );
      return;
    }
    if (ev.target.matches("[data-notify-ofd]")) {
      const nc = this._notifyConfig();
      this._setNotifications(true, nc.targets, ev.target.checked, nc.shortName);
      return;
    }
    if (ev.target.matches("[data-notify-short]")) {
      const nc = this._notifyConfig();
      this._setNotifications(true, nc.targets, nc.ofdOnly, ev.target.checked);
      return;
    }
    if (ev.target.matches("[data-notify-target]")) {
      const nc = this._notifyConfig();
      const targets = Array.from(
        this.querySelectorAll("[data-notify-target]")
      )
        .filter((el) => el.checked)
        .map((el) => el.getAttribute("data-notify-target"));
      this._setNotifications(true, targets, nc.ofdOnly, nc.shortName);
      return;
    }
    const pick = ev.target.closest("[data-carrier]");
    if (pick) {
      this._hass.callService("paketverfolgung", "set_tracking_carrier", {
        tracking_number: pick.getAttribute("data-carrier"),
        carrier: pick.value,
      });
      pick.disabled = true;
      return;
    }
    const dir = ev.target.closest("[data-direction]");
    if (dir) {
      this._hass.callService("paketverfolgung", "set_tracking_direction", {
        tracking_number: dir.getAttribute("data-direction"),
        direction: dir.value,
      });
      dir.disabled = true;
      return;
    }
    const rename = ev.target.closest("[data-name]");
    if (rename) {
      this._hass.callService("paketverfolgung", "set_tracking_name", {
        tracking_number: rename.getAttribute("data-name"),
        name: rename.value.trim(),
      });
      rename.disabled = true;
    }
  }

  async _onSubmit(ev) {
    ev.preventDefault();
    const value = (this._draftTracking || "").trim();
    if (!value || this._addBusy) return;
    this._addBusy = true;
    this._addResult = null;
    this._sig = null;
    this._render(true);
    try {
      const resp = await this._hass.callService(
        "paketverfolgung",
        "add_tracking_number",
        { tracking_number: value },
        undefined,
        true,
        true
      );
      const added =
        resp && resp.response ? resp.response.added : undefined;
      this._addResult = {
        ok: true,
        text:
          added === false
            ? `${value} wird bereits verfolgt.`
            : `${value} hinzugefügt. Anbieter wird bei der nächsten Aktualisierung erkannt.`,
      };
      this._draftTracking = "";
    } catch (err) {
      this._addResult = {
        ok: false,
        text: `Konnte nicht hinzugefügt werden: ${err.message || err}`,
      };
    } finally {
      this._addBusy = false;
      this._sig = null;
      this._render(true);
    }
  }

  _navigate(path) {
    history.pushState(null, "", path);
    this.dispatchEvent(
      new CustomEvent("location-changed", { bubbles: true, composed: true })
    );
    this._render();
  }
}

/* ---------- helpers ---------- */

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function directionLabel(dir) {
  return (
    {
      send: "Gesendet",
      receive: "Empfangen",
      incoming: "Empfangen",
      return: "Retoure",
      OUTBOUND: "Gesendet",
      INBOUND: "Empfangen",
      ANKOMMEND: "Empfangen",
      EINGEHEND: "Empfangen",
      AUSGEHEND: "Gesendet",
    }[dir] || dir
  );
}

function fmtTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function fmtCountdown(target) {
  if (!target || Number.isNaN(target.getTime())) return "– unbekannt";
  const ms = target.getTime() - Date.now();
  if (ms <= 5000) return "läuft …";
  const min = Math.round(ms / 60000);
  if (min < 1) return "in unter 1 Min";
  return `in ~${min} Min (${target.toLocaleTimeString("de-DE", {
    hour: "2-digit",
    minute: "2-digit",
  })})`;
}

function fmtDateTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtShort(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const date = d.toLocaleDateString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    ...(d.getFullYear() === new Date().getFullYear() ? {} : { year: "2-digit" }),
  });
  const time = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  return `${date} ${time}`;
}

function fmtRelative(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return "gerade eben";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `vor ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `vor ${hours} h`;
  const days = Math.round(hours / 24);
  return `vor ${days} d`;
}

const STYLES = `
  :host { display: block; background: var(--primary-background-color); min-height: 100vh; }
  .pv-toolbar {
    display: flex; align-items: center;
    height: 48px; padding: 4px 8px;
    color: var(--primary-text-color);
  }
  .pv-content {
    max-width: 980px; margin: 8px auto 20px; padding: 28px 16px 20px;
    background: var(--ha-card-background, var(--card-background-color));
    border: 1px solid var(--ha-card-border-color, var(--divider-color));
    border-radius: var(--ha-card-border-radius, 16px);
    box-shadow: var(--ha-card-box-shadow, none);
  }
  @media (max-width: 600px) { .pv-content { margin: 8px 10px 20px; } }

  .pv-header {
    display: flex; align-items: center; gap: 16px; margin-bottom: 24px;
  }
  .pv-logo { flex: none; width: 58px; height: 58px; display: block; }
  .pv-logo svg { width: 100%; height: 100%; }
  .pv-brand { font-size: 23px; font-weight: 600; color: var(--primary-text-color); }

  .pv-stats {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px;
  }
  .pv-stat {
    background: var(--card-background-color); border-radius: 12px; padding: 14px;
    border: 1px solid var(--divider-color); text-align: center;
  }
  .pv-stat-value { font-size: 26px; font-weight: 600; color: var(--primary-text-color); }
  .pv-stat-label { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }

  .pv-next {
    display: flex; align-items: center; gap: 8px; width: 100%; cursor: pointer;
    background: transparent; border: none; padding: 4px 2px 12px; text-align: left;
    font-size: 13px; color: var(--secondary-text-color);
  }
  .pv-next ha-icon { --mdc-icon-size: 18px; color: var(--primary-color); }
  .pv-next b { font-weight: 500; color: var(--primary-text-color); }
  .pv-next-now { margin-left: auto; color: var(--primary-color); }
  .pv-next:hover .pv-next-now { text-decoration: underline; }
  .pv-next.busy { opacity: .6; pointer-events: none; }
  .pv-next.busy .pv-next-now { display: none; }

  .pv-add { display: flex; gap: 8px; margin-bottom: 12px; }
  .pv-add input {
    flex: 1; padding: 10px 12px; border-radius: 8px; font-size: 14px;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color);
  }
  .pv-add button, .pv-actions button {
    padding: 10px 16px; border: none; border-radius: 8px; cursor: pointer;
    background: var(--primary-color); color: var(--text-primary-color, #fff); font-size: 14px;
  }
  .pv-add button[disabled] { opacity: .6; cursor: default; }

  .pv-note {
    font-size: 13px; color: var(--secondary-text-color); margin-bottom: 12px;
    padding: 8px 12px; border-radius: 8px; background: var(--card-background-color);
  }
  .pv-note.ok { color: var(--success-color, #2e7d32); }
  .pv-note.err { color: var(--error-color, #c62828); }

  .pv-list { display: flex; flex-direction: column; gap: 8px; }
  .pv-row {
    display: flex; align-items: flex-start; gap: 10px; width: 100%; text-align: left;
    background: var(--card-background-color); border-radius: 12px;
    border: 1px solid var(--divider-color);
    padding: 12px; cursor: pointer; color: var(--primary-text-color);
  }
  .pv-row:hover { background: var(--secondary-background-color); }
  .pv-row-num {
    flex: none; width: 18px; text-align: center; font-size: 13px; line-height: 20px;
    font-weight: 500; color: var(--secondary-text-color);
  }
  .pv-row-icon { flex: none; color: var(--primary-color); --mdc-icon-size: 20px; margin-top: 1px; }
  .pv-row-main { flex: 1 1 auto; min-width: 0; }
  .pv-row-name {
    font-size: 17px; font-weight: 500; margin-bottom: 1px; line-height: 1.3;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pv-row-id {
    font-weight: 500; font-size: 14px; line-height: 1.35;
    overflow-wrap: anywhere; word-break: break-all;
  }
  /* When the row has a custom name, the tracking number becomes a quiet subtitle. */
  .pv-row-main.titled .pv-row-id {
    font-size: 12px; font-weight: 400; color: var(--secondary-text-color);
  }
  .pv-row-meta {
    font-size: 12px; margin-top: 3px; line-height: 1.4; overflow-wrap: anywhere;
  }
  .pv-row-status { color: var(--primary-text-color); }
  .pv-row-status.done { color: var(--success-color, #2e7d32); }
  .pv-row-dim { color: var(--secondary-text-color); }
  .pv-row-dim::before { content: " · "; }
  .pv-chevron { flex: none; color: var(--secondary-text-color); margin-top: 1px; }

  .pv-empty { color: var(--secondary-text-color); padding: 32px 8px; text-align: center; }

  .pv-archive { margin-top: 16px; }
  .pv-archive > summary {
    cursor: pointer; list-style: none; user-select: none;
    padding: 11px 14px; font-size: 13px; color: var(--secondary-text-color);
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    border-radius: 10px;
  }
  .pv-archive > summary::-webkit-details-marker { display: none; }
  .pv-archive > summary::before { content: "▶"; margin-right: 8px; font-size: 10px; }
  .pv-archive[open] > summary::before { content: "▼"; }
  .pv-archive-list { margin-top: 8px; opacity: .8; }

  .pv-settings-box { margin-top: 24px; }
  .pv-settings-box > summary {
    cursor: pointer; list-style: none; user-select: none;
    padding: 11px 14px; font-size: 14px; color: var(--secondary-text-color);
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    border-radius: 10px;
  }
  .pv-settings-box > summary::-webkit-details-marker { display: none; }
  .pv-settings-box > summary::before { content: "▶"; margin-right: 8px; font-size: 10px; }
  .pv-settings-box[open] > summary::before { content: "▼"; }
  .pv-settings-body {
    margin-top: 10px; padding: 14px; border-radius: 10px;
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    display: flex; flex-direction: column; gap: 12px;
  }
  .pv-settings {
    display: flex; align-items: center; gap: 8px; width: 100%; cursor: pointer;
    background: var(--secondary-background-color); color: var(--primary-text-color);
    border: 1px solid var(--divider-color); border-radius: 10px; padding: 12px 14px;
    font-size: 14px; text-align: left;
  }
  .pv-settings:hover { filter: brightness(1.1); }
  .pv-settings ha-icon { color: var(--primary-color); }
  .pv-set-title {
    font-size: 13px; font-weight: 600; color: var(--primary-text-color);
    margin-top: 4px;
  }
  .pv-switch, .pv-check {
    display: flex; align-items: center; gap: 10px; font-size: 14px;
    color: var(--primary-text-color); cursor: pointer;
  }
  .pv-switch input, .pv-check input { width: 18px; height: 18px; flex: none; accent-color: var(--primary-color); }
  .pv-switch-sub { margin: -4px 0 0 28px; font-size: 13px; color: var(--secondary-text-color); }
  .pv-switch-sub em { font-style: normal; opacity: 0.7; }
  .pv-test-row { display: flex; align-items: center; gap: 10px; margin: 2px 0 0 28px; }
  .pv-mini-btn {
    padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: 13px;
    border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--primary-text-color);
  }
  .pv-mini-btn[disabled] { opacity: .5; cursor: default; }
  .pv-test-msg { font-size: 12px; color: var(--secondary-text-color); }
  .pv-version {
    margin-top: 10px; text-align: right; font-size: 11px;
    color: var(--secondary-text-color); opacity: 0.6; font-variant-numeric: tabular-nums;
  }
  .pv-set-hint { font-size: 12px; color: var(--secondary-text-color); }
  .pv-set-hint code {
    background: var(--secondary-background-color); padding: 1px 4px; border-radius: 4px;
  }
  .pv-picker > summary {
    cursor: pointer; list-style: none; user-select: none;
    display: flex; align-items: baseline; gap: 8px;
    padding: 10px 12px; font-size: 13px;
    background: var(--secondary-background-color);
    border: 1px solid var(--divider-color); border-radius: 10px;
  }
  .pv-picker > summary::-webkit-details-marker { display: none; }
  .pv-picker > summary::before { content: "▶"; font-size: 10px; color: var(--secondary-text-color); }
  .pv-picker[open] > summary::before { content: "▼"; }
  .pv-picker-label { color: var(--primary-text-color); font-weight: 500; flex: none; }
  .pv-picker-value {
    color: var(--secondary-text-color); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .pv-picker-body {
    margin-top: 8px; border: 1px solid var(--divider-color); border-radius: 10px;
    background: var(--card-background-color); overflow: hidden;
  }
  .pv-picker-filter {
    width: 100%; box-sizing: border-box; border: none; outline: none;
    padding: 10px 12px; font-size: 13px;
    background: transparent; color: var(--primary-text-color);
    border-bottom: 1px solid var(--divider-color);
  }
  .pv-notify-list {
    display: flex; flex-direction: column;
    max-height: 260px; overflow-y: auto; padding: 8px 12px;
  }
  .pv-notify-list .pv-check { padding: 6px 0; font-size: 13px; }
  .pv-notify-list .pv-check[hidden] { display: none; }
  .pv-footer-hint { font-size: 12px; color: var(--secondary-text-color); }
  .pv-hint-tight { margin: -6px 2px 0; line-height: 1.4; }

  .pv-back {
    display: inline-block; margin-bottom: 16px; cursor: pointer;
    color: var(--primary-color); font-size: 14px; text-decoration: none;
  }
  .pv-detail-head { display: flex; gap: 16px; align-items: flex-start; margin-bottom: 16px; }
  .pv-detail-icon { color: var(--primary-color); --mdc-icon-size: 44px; }
  .pv-detail-name { font-size: 20px; font-weight: 600; color: var(--primary-text-color); }
  .pv-detail-status { font-size: 15px; color: var(--secondary-text-color); margin-top: 2px; }
  .pv-detail-status.done { color: var(--success-color, #2e7d32); }
  .pv-detail-updated { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }

  .pv-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
  .pv-linkbtn {
    padding: 10px 16px; border-radius: 8px; font-size: 14px; text-decoration: none;
    background: var(--secondary-background-color); color: var(--primary-text-color);
    border: 1px solid var(--divider-color);
  }
  .pv-actions button.pv-delete {
    background: transparent; color: var(--error-color, #c62828);
    border: 1px solid var(--error-color, #c62828);
  }

  .pv-carrier, .pv-rename {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    font-size: 13px; color: var(--secondary-text-color); margin-bottom: 12px;
  }
  .pv-carrier { margin-bottom: 20px; }
  .pv-carrier select, .pv-rename input {
    padding: 8px 10px; border-radius: 8px; font-size: 13px;
    border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color);
  }
  .pv-rename input { flex: 1; min-width: 160px; }

  .pv-meta {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
    background: var(--card-background-color); border-radius: 12px; padding: 16px;
    border: 1px solid var(--divider-color); margin-bottom: 24px;
  }
  .pv-meta-key { font-size: 12px; color: var(--secondary-text-color); }
  .pv-meta-val { font-size: 14px; color: var(--primary-text-color); margin-top: 2px; overflow-wrap: anywhere; }

  .pv-section-title { font-size: 16px; font-weight: 600; color: var(--primary-text-color); margin-bottom: 12px; }

  .pv-timeline { list-style: none; margin: 0; padding: 0; }
  .pv-ev { display: flex; gap: 14px; padding-bottom: 18px; position: relative; }
  .pv-ev:not(:last-child)::before {
    content: ""; position: absolute; left: 5px; top: 14px; bottom: 0; width: 2px;
    background: var(--divider-color);
  }
  .pv-ev-dot {
    width: 12px; height: 12px; border-radius: 50%; margin-top: 3px; flex: none;
    background: var(--disabled-text-color, #9e9e9e);
  }
  .pv-ev.current .pv-ev-dot { background: var(--primary-color); }
  .pv-ev-status { font-size: 14px; color: var(--primary-text-color); }
  .pv-ev.current .pv-ev-status { font-weight: 600; }
  .pv-ev-date { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
`;

if (!customElements.get("paketverfolgung-panel")) {
  customElements.define("paketverfolgung-panel", PaketverfolgungPanel);
}
