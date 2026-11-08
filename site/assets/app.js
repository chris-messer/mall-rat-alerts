// Progressive enhancement only. The forms post normally without JavaScript.
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  var SLUG = /^[a-z0-9][a-z0-9-]{2,63}$/;
  var CONSENT_KEY = "mraAnalyticsConsent";
  var campaignId = "fob-twilight-town-mall-2026";
  var posthog = null;

  // ------------------------------------------------------------- analytics

  function storedChoice() {
    try { return window.localStorage.getItem(CONSENT_KEY); } catch (e) { return null; }
  }

  function storeChoice(value) {
    try { window.localStorage.setItem(CONSENT_KEY, value); } catch (e) { /* private mode */ }
  }

  // array.js replaces window.posthog with the real client once loaded; calls on the old stub are dropped.
  function client() { return posthog && (window.posthog || posthog); }

  function track(event, props) {
    var ph = client();
    if (!ph) return;
    var p = { campaign_id: campaignId };
    for (var k in props || {}) p[k] = props[k];
    ph.capture(event, p, { send_instantly: true });
  }

  // Official PostHog loader stub (queues calls until array.js loads), kept out of inline HTML for the CSP.
  function loadPostHog(key, host) {
    !function (t, e) {
      var o, n, p, r;
      e.__SV || (window.posthog = e, e._i = [], e.init = function (i, s, a) {
        function g(t, e) { var o = e.split("."); 2 == o.length && (t = t[o[0]], e = o[1]), t[e] = function () { t.push([e].concat(Array.prototype.slice.call(arguments, 0))); }; }
        (p = t.createElement("script")).type = "text/javascript", p.crossOrigin = "anonymous", p.async = !0,
        p.src = s.api_host.replace(".i.posthog.com", "-assets.i.posthog.com") + "/static/array.js",
        (r = t.getElementsByTagName("script")[0]).parentNode.insertBefore(p, r);
        var u = e;
        for (void 0 !== a ? u = e[a] = [] : a = "posthog", u.people = u.people || [], u.toString = function (t) { var e = "posthog"; return "posthog" !== a && (e += "." + a), t || (e += " (stub)"), e; }, u.people.toString = function () { return u.toString(1) + ".people (stub)"; }, o = "capture identify alias people.set people.set_once set_config register register_once unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled onFeatureFlags getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group onSessionId".split(" "), n = 0; n < o.length; n++) g(u, o[n]);
        e._i.push([i, s, a]);
      }, e.__SV = 1);
    }(document, window.posthog || []);

    window.posthog.init(key, {
      api_host: host,
      persistence: "memory",
      person_profiles: "identified_only",
      autocapture: false,
      capture_pageview: true,
      capture_pageleave: false,
      disable_session_recording: true,
      disable_surveys: true,
      capture_dead_clicks: false,
      capture_performance: false,
      respect_dnt: true
    });
    window.posthog.register({ campaign_id: campaignId, site: "mall-rat-alerts" });
    posthog = window.posthog;
  }

  function showNotice() {
    var bar = document.createElement("div");
    bar.className = "analytics-notice";
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Analytics notice");
    bar.innerHTML = '<p>We use cookieless analytics (PostHog) to see how this site is used. We never send your phone number. <a href="/privacy/#analytics">Learn more</a></p>';
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn btn-small";
    ok.textContent = "OK";
    var no = document.createElement("button");
    no.type = "button";
    no.className = "btn btn-small btn-ghost";
    no.textContent = "No thanks";
    ok.addEventListener("click", function () { storeChoice("accepted"); bar.remove(); });
    no.addEventListener("click", function () {
      storeChoice("declined");
      if (client()) client().opt_out_capturing();
      posthog = null;
      bar.remove();
    });
    bar.appendChild(ok);
    bar.appendChild(no);
    document.body.appendChild(bar);
  }

  function initAnalytics(cfg) {
    var ph = (cfg && cfg.posthog) || {};
    if (!ph.apiKey || !ph.host) return;
    if (navigator.doNotTrack === "1" || navigator.globalPrivacyControl === true) return;
    var choice = storedChoice();
    if (choice === "declined") return;
    loadPostHog(ph.apiKey, ph.host);
    if (!choice) showNotice();
  }

  // ----------------------------------------------------------------- forms

  function setField(form, name, value) {
    var el = form.elements[name];
    if (el && value) el.value = String(value).slice(0, 300);
  }

  // successUrl null = show the success message inline (the sign-up form); otherwise redirect.
  function enhance(form, kind, successUrl) {
    if (!form) return;
    var device = kind === "subscribe" ? { device: "desktop" } : {};
    setField(form, "form_rendered_at", Math.floor(Date.now() / 1000));
    setField(form, "landing_path", window.location.pathname);
    setField(form, "referrer", document.referrer);
    ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"].forEach(function (k) {
      setField(form, k, params.get(k));
    });
    var requested = params.get("campaign_id") || params.get("c");
    if (requested && SLUG.test(requested)) setField(form, "campaign_id", requested);
    if (form.elements.campaign_id) campaignId = form.elements.campaign_id.value;

    var status = form.querySelector(".form-status");
    form.addEventListener("submit", function (ev) {
      if (!window.fetch || !form.checkValidity()) return;
      ev.preventDefault();
      var data = {};
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el.name) return;
        if (el.type === "checkbox") data[el.name] = el.checked;
        else data[el.name] = el.value;
      });
      track(kind + "_submit", { utm_source: data.utm_source || undefined, device: device.device });
      if (status) status.className = "form-status";
      var button = form.querySelector("button[type=submit]");
      if (button) button.disabled = true;
      if (status) status.textContent = "Sending...";
      fetch(form.getAttribute("action"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
        credentials: "same-origin"
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          if (res.ok) {
            track(kind + "_success", { result: body.status, device: device.device });
            if (successUrl) {
              setTimeout(function () { window.location.assign(successUrl); }, client() ? 300 : 0);
            } else {
              if (status) { status.className = "form-status ok"; status.textContent = body.message || "Thanks! Check your phone."; }
              if (button) button.disabled = true;
            }
            return;
          }
          track(kind + "_error", { error: body.error || "http_" + res.status, http_status: res.status, device: device.device });
          if (status) status.textContent = body.message || "Something went wrong. Please try again.";
          if (button) button.disabled = false;
        });
      }).catch(function () {
        track(kind + "_error", { error: "network", device: device.device });
        if (status) status.textContent = "Network error. Please try again.";
        if (button) button.disabled = false;
      });
    });
  }

  // ------------------------------------------------------- recent alerts

  function renderAlerts() {
    var board = document.getElementById("recent-alerts");
    var list = document.getElementById("alert-list");
    if (!board || !list) return;
    var cid = board.getAttribute("data-campaign") || campaignId;
    var fmt = null;
    try {
      fmt = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Los_Angeles", weekday: "short", month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZoneName: "short"
      });
    } catch (e) { /* fall back to the raw timestamp */ }

    function note(cls, text) {
      list.textContent = "";
      var li = document.createElement("li");
      li.className = cls;
      li.textContent = text;
      list.appendChild(li);
    }

    if (!window.fetch) { note("alert-empty", "Recent alerts need a newer browser."); return; }
    fetch("/api/alerts?campaign_id=" + encodeURIComponent(cid) + "&limit=50", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (data) {
        var rows = ((data && data.alerts) || []).slice();
        if (!rows.length) { note("alert-empty", "No alerts yet. Alerts are live and go out by text when the site changes; they also show up here."); return; }
        rows.sort(function (x, y) { return String(y.sent_at || "").localeCompare(String(x.sent_at || "")); });
        list.textContent = "";
        rows.forEach(function (a) {
          var li = document.createElement("li");
          var t = document.createElement("time");
          t.setAttribute("datetime", a.sent_at);
          var d = new Date(a.sent_at);
          t.textContent = fmt && !isNaN(d) ? fmt.format(d) : a.sent_at;
          var p = document.createElement("p");
          p.textContent = a.message || "";
          li.appendChild(t);
          li.appendChild(p);
          list.appendChild(li);
        });
      })
      .catch(function () { note("alert-error", "Could not load recent alerts right now. Please try again later."); });
  }

  renderAlerts();

  enhance(document.getElementById("subscribe-form"), "subscribe", null);
  enhance(document.getElementById("unsubscribe-form"), "unsubscribe", "/unsubscribe/done/");

  // ---------------------------------------------------- text-to-join pill

  // The sms: link does the work on its own; tracking must never block or cancel the navigation.
  Array.prototype.forEach.call(document.querySelectorAll("[data-sms-cta]"), function (link) {
    link.addEventListener("click", function () {
      try { track("subscribe_sms_click", { placement: link.getAttribute("data-sms-cta"), device: "mobile" }); } catch (e) { /* ignore */ }
    });
  });

  // Tip jar link works without JS; tracking must never block or cancel the navigation.
  Array.prototype.forEach.call(document.querySelectorAll("[data-tip-cta]"), function (link) {
    link.addEventListener("click", function () {
      try {
        track("tip_click", {
          campaign_id: campaignId,
          placement: link.getAttribute("data-tip-cta") || "top_banner"
        });
      } catch (e) { /* ignore */ }
    });
  });

  // Discord invite works without JS; tracking must never block or cancel the navigation.
  Array.prototype.forEach.call(document.querySelectorAll("[data-discord-cta]"), function (link) {
    link.addEventListener("click", function () {
      try {
        track("discord_join_click", {
          campaign_id: campaignId,
          placement: link.getAttribute("data-discord-cta") || "hero"
        });
      } catch (e) { /* ignore */ }
    });
  });

  // SMS details open: track once per open; the native details toggle works without JS.
  Array.prototype.forEach.call(document.querySelectorAll("details.sms-details"), function (el) {
    el.addEventListener("toggle", function () {
      if (!el.open) return;
      try {
        track("sms_details_open", {
          campaign_id: campaignId,
          placement: "hero"
        });
      } catch (e) { /* ignore */ }
    });
  });

  if (window.fetch) {
    fetch("/config.json", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(initAnalytics)
      .catch(function () { /* analytics stays off */ });
  }
})();
