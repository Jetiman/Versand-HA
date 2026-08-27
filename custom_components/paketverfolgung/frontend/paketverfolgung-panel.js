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

const OUT_FOR_DELIVERY_ICON = "mdi:truck-delivery";
const DELIVERED_ICONS = new Set([
  "mdi:package-variant-closed-check",
]);

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
    this._onPopState = () => this._render();
  }

  connectedCallback() {
    window.addEventListener("popstate", this._onPopState);
    this.addEventListener("click", this._onClick);
    this.addEventListener("input", this._onInput);
    this.addEventListener("submit", this._onSubmit);
    this._render(true);
  }

  disconnectedCallback() {
    window.removeEventListener("popstate", this._onPopState);
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

  _shipments() {
    if (!this._hass) return [];
    const out = [];
    for (const stateObj of Object.values(this._hass.states)) {
      if (!stateObj.entity_id.startsWith("sensor.")) continue;
      const a = stateObj.attributes || {};
      if (!a.tracking_id) continue;
      const url = a.tracking_url || "";
      const provider = url.includes("dpd") ? "DPD" : "DHL";
      const delivered =
        a.delivered === true ||
        a.progress === 5 ||
        DELIVERED_ICONS.has(a.icon);
      out.push({
        entity_id: stateObj.entity_id,
        name: a.friendly_name || stateObj.entity_id,
        status: stateObj.state,
        icon: a.icon || "mdi:package-variant-closed",
        tracking_id: String(a.tracking_id),
        provider,
        direction: a.direction || null,
        delivery_from: a.delivery_window_from || null,
        delivery_to: a.delivery_window_to || null,
        tracking_url: url || null,
        events: Array.isArray(a.events) ? a.events : [],
        delivered,
        out_for_delivery:
          a.icon === OUT_FOR_DELIVERY_ICON ||
          ["OUT_FOR_DELIVERY", "IN_DELIVERY"].includes(a.status_id),
        last_updated: stateObj.last_updated,
        last_changed: stateObj.last_changed,
      });
    }
    out.sort((x, y) => {
      if (x.delivered !== y.delivered) return x.delivered ? 1 : -1;
      return (y.last_changed || "").localeCompare(x.last_changed || "");
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
    const sig = JSON.stringify({
      entityId,
      busy: this._addBusy,
      result: this._addResult,
      items: shipments.map((s) => [
        s.entity_id,
        s.status,
        s.last_updated,
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
    const inTransit = total - delivered;
    let outForDelivery = shipments.filter((s) => s.out_for_delivery && !s.delivered)
      .length;
    const combined = this._hass.states["sensor.heute_in_zustellung"];
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
      ? shipments.map((s) => this._rowHtml(s)).join("")
      : `<div class="pv-empty">Noch keine Sendungen. Füge unten eine DHL-Sendungsnummer hinzu
          oder richte DPD ein.</div>`;

    let resultMsg = "";
    if (this._addResult) {
      resultMsg = `<div class="pv-note ${this._addResult.ok ? "ok" : "err"}">${esc(
        this._addResult.text
      )}</div>`;
    }

    return `
      <div class="pv-stats">${stats}</div>
      ${
        canAdd
          ? `<form class="pv-add" autocomplete="off">
              <input name="tracking" type="text" inputmode="numeric"
                placeholder="DHL-Sendungsnummer hinzufügen" />
              <button type="submit" ${this._addBusy ? "disabled" : ""}>
                ${this._addBusy ? "…" : "Hinzufügen"}
              </button>
            </form>
            ${resultMsg}`
          : ""
      }
      <div class="pv-list">${rows}</div>
    `;
  }

  _rowHtml(s) {
    return `
      <button class="pv-row" data-entity="${esc(s.entity_id)}">
        <ha-icon class="pv-row-icon" icon="${esc(s.icon)}"></ha-icon>
        <div class="pv-row-main">
          <div class="pv-row-name">${esc(s.name)}</div>
          <div class="pv-row-sub">${esc(s.provider)} · ${esc(s.tracking_id)}</div>
        </div>
        <div class="pv-row-status ${s.delivered ? "done" : ""}">${esc(s.status)}</div>
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
      ["Anbieter", s.provider],
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

    let timeline;
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
    } else {
      timeline = `
        <li class="pv-ev current">
          <div class="pv-ev-dot"></div>
          <div class="pv-ev-body">
            <div class="pv-ev-status">${esc(s.status)}</div>
            <div class="pv-ev-date">${esc(fmtRelative(s.last_changed))}</div>
          </div>
        </li>`;
    }

    const noHistoryNote =
      !s.events.length && s.provider === "DPD"
        ? `<div class="pv-note">DPD liefert über diese Schnittstelle nur den aktuellen Status,
            keinen vollständigen Verlauf.</div>`
        : "";

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
      </div>

      <div class="pv-meta">${meta}</div>

      <div class="pv-section-title">Sendungsverlauf</div>
      ${noHistoryNote}
      <ul class="pv-timeline">${timeline}</ul>
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
    const refresh = ev.target.closest("[data-refresh]");
    if (refresh) {
      this._hass.callService("homeassistant", "update_entity", {
        entity_id: refresh.getAttribute("data-refresh"),
      });
      refresh.disabled = true;
      refresh.textContent = "Wird aktualisiert …";
    }
  }

  _onInput(ev) {
    if (ev.target.name === "tracking") {
      this._draftTracking = ev.target.value;
      this._addResult = null;
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
            : `${value} hinzugefügt. Der Sensor erscheint nach der nächsten Abfrage.`,
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
    display: flex; align-items: center; gap: 14px; width: 100%; text-align: left;
    background: var(--card-background-color); border: none; border-radius: 12px;
    padding: 12px 14px; cursor: pointer; color: var(--primary-text-color);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
  }
  .pv-row:hover { background: var(--secondary-background-color); }
  .pv-row-icon { color: var(--primary-color); --mdc-icon-size: 28px; }
  .pv-row-main { flex: 1; min-width: 0; }
  .pv-row-name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pv-row-sub { font-size: 12px; color: var(--secondary-text-color); }
  .pv-row-status {
    font-size: 13px; color: var(--secondary-text-color); text-align: right;
    max-width: 40%;
  }
  .pv-row-status.done { color: var(--success-color, #2e7d32); }
  .pv-chevron { color: var(--secondary-text-color); }

  .pv-empty { color: var(--secondary-text-color); padding: 32px 8px; text-align: center; }

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

  .pv-meta {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
    background: var(--card-background-color); border-radius: 12px; padding: 16px;
    margin-bottom: 24px; box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.1));
  }
  .pv-meta-key { font-size: 12px; color: var(--secondary-text-color); }
  .pv-meta-val { font-size: 14px; color: var(--primary-text-color); margin-top: 2px; word-break: break-word; }

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
