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

const PROVIDER_LABELS = { dhl: "DHL", dpd: "DPD", hermes: "Hermes" };

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
      // "last change" of the shipment itself: the newest carrier event if
      // we have one, otherwise when the sensor state last changed.
      const changed =
        (events[0] && events[0].datum) || stateObj.last_changed || null;
      out.push({
        entity_id: stateObj.entity_id,
        name: a.friendly_name || stateObj.entity_id,
        status: stateObj.state,
        icon: a.icon || "mdi:package-variant-closed",
        tracking_id: String(a.tracking_id),
        provider: PROVIDER_LABELS[carrier] || "?",
        carrier,
        group,
        direction: a.direction || null,
        delivery_from: a.delivery_window_from || null,
        delivery_to: a.delivery_window_to || null,
        tracking_url: url || null,
        events,
        delivered,
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

  /* ---------- rendering ---------- */

  _render(force) {
    if (!this._hass) return;
    const shipments = this._shipments();
    const entityId = this._currentEntityId();
    const combined = this._combinedSensor();
    const nextIso = combined && combined.attributes && combined.attributes.next_update;
    this._nextUpdate = nextIso ? new Date(nextIso) : null;
    const sig = JSON.stringify({
      entityId,
      busy: this._addBusy,
      result: this._addResult,
      nextIso: nextIso || null,
      items: shipments.map((s) => [
        s.entity_id,
        s.name,
        s.status,
        s.group,
        s.carrier,
        s.forced,
        s.changed,
        s.events.length,
      ]),
    });
    if (!force && sig === this._sig) return;
    this._sig = sig;

    const body = entityId
      ? this._detailHtml(shipments.find((s) => s.entity_id === entityId), entityId)
      : this._listHtml(shipments);

    this.innerHTML = `
      <style>${STYLES}</style>
      <div class="pv-toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="pv-title">Paketverfolgung</div>
      </div>
      <div class="pv-content">${body}</div>
    `;

    const menuBtn = this.querySelector("ha-menu-button");
    if (menuBtn) {
      menuBtn.hass = this._hass;
      menuBtn.narrow = this._narrow;
    }
    const input = this.querySelector('input[name="tracking"]');
    if (input && this._draftTracking) input.value = this._draftTracking;
  }

  _listHtml(shipments) {
    const total = shipments.length;
    const delivered = shipments.filter((s) => s.delivered).length;
    const inReview = shipments.filter((s) => s.group === "unknown").length;
    const inTransit = total - delivered - inReview;
    let outForDelivery = shipments.filter((s) => s.out_for_delivery && !s.delivered)
      .length;
    const combined = this._combinedSensor();
    if (combined && !Number.isNaN(Number(combined.state))) {
      outForDelivery = Number(combined.state);
    }

    const stats = [
      ["Sendungen", total],
      ["Unterwegs", inTransit],
      ["Heute in Zustellung", outForDelivery],
      ["Zugestellt", delivered],
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
      : `<div class="pv-empty">Noch keine Sendungen. Füge unten eine Sendungsnummer hinzu
          oder richte das DPD-Konto ein.</div>`;

    let resultMsg = "";
    if (this._addResult) {
      resultMsg = `<div class="pv-note ${this._addResult.ok ? "ok" : "err"}">${esc(
        this._addResult.text
      )}</div>`;
    }

    return `
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

      <div class="pv-footer">
        <button class="pv-settings" data-nav="/config/integrations/integration/paketverfolgung">
          <ha-icon icon="mdi:cog-outline"></ha-icon>
          Einstellungen (DPD-Login, PLZ, Sendungsnummern)
        </button>
        <div class="pv-footer-hint">
          Öffnet die Integration – dort beim jeweiligen Eintrag auf das Zahnrad
          für PLZ und Sendungsnummern, oder „Eintrag hinzufügen“ für den DPD-Login.
        </div>
      </div>
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
        <div class="pv-row-main">
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
      ["Sendungsnummer", s.tracking_id],
      ["Richtung", directionLabel(s.direction)],
      [
        "Zustellzeitfenster",
        s.delivery_from
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
      note = `<div class="pv-note">Diese Sendung wird noch geprüft – kein Anbieter (DHL, DPD, Hermes)
        hat bisher Daten dazu geliefert. Sie bleibt in der Liste und wird bei jeder Aktualisierung
        erneut geprüft, bis du sie löschst oder oben den Anbieter manuell festlegst.</div>`;
    } else if (!s.events.length && (s.provider === "DPD" || s.provider === "Hermes")) {
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
    }
  }

  _onChange(ev) {
    const pick = ev.target.closest("[data-carrier]");
    if (pick) {
      this._hass.callService("paketverfolgung", "set_tracking_carrier", {
        tracking_number: pick.getAttribute("data-carrier"),
        carrier: pick.value,
      });
      pick.disabled = true;
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
      return: "Retoure",
      OUTBOUND: "Gesendet",
      INBOUND: "Empfangen",
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
    display: flex; align-items: center; gap: 16px;
    height: var(--header-height, 56px); padding: 0 16px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, #fff);
    font-size: 20px; font-weight: 400;
    position: sticky; top: 0; z-index: 2;
  }
  .pv-content { max-width: 980px; margin: 0 auto; padding: 16px; }

  .pv-stats {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;
  }
  @media (max-width: 600px) { .pv-stats { grid-template-columns: repeat(2, 1fr); } }
  .pv-stat {
    background: var(--card-background-color); border-radius: 12px; padding: 14px;
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1)); text-align: center;
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
    background: var(--card-background-color); border: none; border-radius: 12px;
    padding: 12px; cursor: pointer; color: var(--primary-text-color);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
  }
  .pv-row:hover { background: var(--secondary-background-color); }
  .pv-row-num {
    flex: none; width: 18px; text-align: center; font-size: 13px; line-height: 20px;
    font-weight: 500; color: var(--secondary-text-color);
  }
  .pv-row-icon { flex: none; color: var(--primary-color); --mdc-icon-size: 20px; margin-top: 1px; }
  .pv-row-main { flex: 1 1 auto; min-width: 0; }
  .pv-row-name {
    font-weight: 500; margin-bottom: 1px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pv-row-id {
    font-weight: 500; font-size: 14px; line-height: 1.35;
    overflow-wrap: anywhere; word-break: break-all;
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

  .pv-footer { margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--divider-color); }
  .pv-settings {
    display: flex; align-items: center; gap: 8px; width: 100%; cursor: pointer;
    background: var(--card-background-color); color: var(--primary-text-color);
    border: 1px solid var(--divider-color); border-radius: 10px; padding: 12px 14px;
    font-size: 14px; text-align: left;
  }
  .pv-settings:hover { background: var(--secondary-background-color); }
  .pv-settings ha-icon { color: var(--primary-color); }
  .pv-footer-hint { font-size: 12px; color: var(--secondary-text-color); margin-top: 8px; }

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
    margin-bottom: 24px; box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
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
