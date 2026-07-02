#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auditeur web — robot de test automatique pour n'importe quel site.

Explore un site (crawl interne), puis pour chaque page mesure :
performance (Core Web Vitals), SEO, accessibilité (axe-core + checks manuels),
responsive (overflow horizontal + éléments coupables), sécurité (en-têtes,
cookies, contenu mixte), liens cassés, erreurs console / JS, et formulaires.

Génère un rapport JSON complet + un rapport HTML lisible avec scores et
priorisation des problèmes, plus des captures d'écran.

Usage minimal :
    python auditeur.py https://monsite.com

Quelques options utiles :
    python auditeur.py https://monsite.com --max-pages 50 --concurrence 6
    python auditeur.py https://monsite.com --only seo,performance
    python auditeur.py https://monsite.com --mobile
    python auditeur.py https://monsite.com --soumettre-formulaires   # ⚠️

Dépendances : playwright (+ chromium). axe-core est embarqué via
axe_playwright_python si présent, sinon récupéré depuis un CDN.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

# Console Windows : forcer l'UTF-8 pour que les emojis/accents du rapport
# ne plantent pas l'encodage (cp1252) à l'affichage.
for _flux in (sys.stdout, sys.stderr):
    try:
        _flux.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("❌ Playwright manquant. Installe-le :  pip install playwright  puis  playwright install chromium")

from rapport_client import generer_rapport_client


# ============================================================
#  CONSTANTES
# ============================================================

VIEWPORTS = {
    "mobile": {"width": 375, "height": 812},
    "tablette": {"width": 768, "height": 1024},
    "bureau": {"width": 1366, "height": 900},
}

# Sévérités et poids utilisés pour le score (sur 100, on retranche le poids).
CRITIQUE, MAJEUR, MINEUR, INFO = "critique", "majeur", "mineur", "info"
POIDS = {CRITIQUE: 25, MAJEUR: 10, MINEUR: 4, INFO: 0}
ORDRE_SEVERITE = {CRITIQUE: 0, MAJEUR: 1, MINEUR: 2, INFO: 3}

CATEGORIES = ["performance", "seo", "accessibilite", "responsive", "securite", "fiabilite"]

AXE_CDN = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"

# Extensions qu'on ne tente pas de crawler comme des pages HTML.
EXT_NON_HTML = {
    ".pdf", ".zip", ".rar", ".gz", ".tar", ".dmg", ".exe", ".apk",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".mov",
    ".css", ".js", ".map", ".json", ".xml", ".rss", ".txt", ".csv",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}


# ============================================================
#  SNIPPETS JAVASCRIPT (injectés dans la page)
# ============================================================

# Injecté AVANT chargement : capture LCP et CLS au fil de l'eau.
JS_VITALS = r"""
window.__vitals = { lcp: null, cls: 0 };
(function () {
  try {
    new PerformanceObserver(function (l) {
      var es = l.getEntries(); var last = es[es.length - 1];
      if (last) window.__vitals.lcp = last.renderTime || last.loadTime || last.startTime || null;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver(function (l) {
      l.getEntries().forEach(function (e) { if (!e.hadRecentInput) window.__vitals.cls += e.value; });
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (e) {}
})();
"""

JS_PERF = r"""
() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paint = performance.getEntriesByType('paint');
  const fcp = paint.find(p => p.name === 'first-contentful-paint');
  const res = performance.getEntriesByType('resource');
  const parType = {};
  let total = 0;
  res.forEach(r => {
    const t = r.initiatorType || 'autre';
    // transferSize=0 pour le cache HTTP -> on retombe sur la taille du corps.
    // (reste 0 pour le cross-origin sans Timing-Allow-Origin : poids = minorant)
    const b = r.transferSize || r.encodedBodySize || r.decodedBodySize || 0;
    parType[t] = (parType[t] || 0) + b;
    total += b;
  });
  const docBytes = nav ? (nav.transferSize || nav.encodedBodySize || 0) : 0;
  if (docBytes) { parType['document'] = (parType['document'] || 0) + docBytes; total += docBytes; }
  const v = window.__vitals || {};
  const num = (x) => (x == null || isNaN(x)) ? null : Math.round(x);
  return {
    ttfb_ms: nav ? num(nav.responseStart) : null,
    fcp_ms: fcp ? num(fcp.startTime) : null,
    lcp_ms: v.lcp != null ? num(v.lcp) : null,
    cls: v.cls != null ? Math.round(v.cls * 1000) / 1000 : null,
    dom_content_loaded_ms: nav ? num(nav.domContentLoadedEventEnd) : null,
    load_ms: nav ? num(nav.loadEventEnd) : null,
    nb_requetes: res.length + (nav ? 1 : 0),
    poids_total_ko: Math.round(total / 1024),
    poids_par_type_ko: Object.fromEntries(
      Object.entries(parType).map(([k, vv]) => [k, Math.round(vv / 1024)])
    ),
  };
}
"""

JS_SEO = r"""
() => {
  const g = (sel, attr) => {
    const el = document.querySelector(sel);
    return el ? (attr ? el.getAttribute(attr) : (el.textContent || '').trim()) : null;
  };
  const imgs = Array.from(document.images);
  const titres = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .map(h => ({ niveau: parseInt(h.tagName[1]), texte: (h.textContent || '').trim().slice(0, 90) }));
  const h1 = titres.filter(t => t.niveau === 1);
  let saut = false, prec = 0;
  for (const t of titres) { if (prec && t.niveau > prec + 1) saut = true; prec = t.niveau; }
  const og = {};
  document.querySelectorAll('meta[property^="og:"]').forEach(m => og[m.getAttribute('property')] = m.getAttribute('content'));
  const texte = document.body ? (document.body.innerText || '').replace(/\s+/g, ' ').trim() : '';
  return {
    titre: g('title'),
    meta_description: g('meta[name="description"]', 'content'),
    canonical: g('link[rel="canonical"]', 'href'),
    meta_robots: g('meta[name="robots"]', 'content'),
    viewport: g('meta[name="viewport"]', 'content'),
    lang: document.documentElement.getAttribute('lang'),
    favicon: !!document.querySelector('link[rel~="icon"]'),
    og_count: Object.keys(og).length,
    twitter_card: g('meta[name="twitter:card"]', 'content'),
    jsonld_count: document.querySelectorAll('script[type="application/ld+json"]').length,
    h1_count: h1.length,
    h1_textes: h1.map(t => t.texte),
    nb_titres: titres.length,
    saut_hierarchie: saut,
    images_total: imgs.length,
    images_sans_alt: imgs.filter(i => !i.hasAttribute('alt')).length,
    images_alt_vide: imgs.filter(i => i.getAttribute('alt') === '').length,
    nb_mots: texte ? texte.split(' ').length : 0,
  };
}
"""

JS_A11Y_MANUEL = r"""
() => {
  const champs = Array.from(document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]), textarea, select'));
  const sansLabel = champs.filter(el => {
    if (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) return false;
    if (el.closest('label')) return false;
    if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title')) return false;
    return true;
  }).length;
  const aImgNommee = (el) => !!el.querySelector('img[alt]:not([alt=""]), svg[aria-label], svg title');
  const boutonsSansNom = Array.from(document.querySelectorAll('button, [role="button"]')).filter(b =>
    !(b.textContent || '').trim() && !b.getAttribute('aria-label') && !b.getAttribute('title') && !aImgNommee(b)).length;
  const liensSansNom = Array.from(document.querySelectorAll('a[href]')).filter(a =>
    !(a.textContent || '').trim() && !a.getAttribute('aria-label') && !a.getAttribute('title') && !aImgNommee(a)).length;
  return {
    html_lang: !!document.documentElement.getAttribute('lang'),
    champs_sans_label: sansLabel,
    boutons_sans_nom: boutonsSansNom,
    liens_sans_nom: liensSansNom,
  };
}
"""

JS_OVERFLOW = r"""
() => {
  const vw = document.documentElement.clientWidth;
  const coupables = [];
  const els = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right > vw + 2) {
      // On ignore les éléments hors flux (position:fixed, off-canvas masqués)
      // qui dépassent visuellement mais NE créent PAS de scroll horizontal réel.
      const st = getComputedStyle(el);
      if (st.position === 'fixed' || st.visibility === 'hidden' || st.opacity === '0') continue;
      // ancêtre qui clippe l'overflow (pattern menu off-canvas) -> pas coupable
      let p = el.parentElement, clip = false;
      while (p && p !== document.body) {
        const ps = getComputedStyle(p);
        if (ps.overflowX === 'hidden' || ps.overflowX === 'clip') { clip = true; break; }
        p = p.parentElement;
      }
      if (clip) continue;
      let id = el.tagName.toLowerCase();
      if (el.id) id += '#' + el.id;
      else if (el.className && typeof el.className === 'string') {
        const c = el.className.trim().split(/\s+/).slice(0, 2).join('.');
        if (c) id += '.' + c;
      }
      coupables.push({ el: id.slice(0, 70), droite: Math.round(r.right), largeur: Math.round(r.width) });
    }
  }
  coupables.sort((a, b) => b.droite - a.droite);
  const vus = new Set(), top = [];
  for (const c of coupables) { if (vus.has(c.el)) continue; vus.add(c.el); top.push(c); if (top.length >= 8) break; }
  return {
    scroll_horizontal: document.documentElement.scrollWidth > vw + 2,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: vw,
    coupables: top,
  };
}
"""

JS_SECU_DOM = r"""
() => {
  // ressources en http:// : on regarde le DOM (currentSrc résout srcset)
  // ET les ressources réellement chargées (capte le dynamique + le CSS).
  const cibles = new Set();
  Array.from(document.querySelectorAll('img,script,iframe,audio,video,source,link[rel="stylesheet"]'))
    .forEach(el => { const u = el.currentSrc || el.src || el.href; if (u) cibles.add(u); });
  try {
    performance.getEntriesByType('resource').forEach(r => { if (r.name) cibles.add(r.name); });
  } catch (e) {}
  const httpRes = Array.from(cibles).filter(u => u.startsWith('http://')).length;
  const blankSansNoopener = Array.from(document.querySelectorAll('a[target="_blank"]'))
    .filter(a => !/noopener|noreferrer/.test(a.getAttribute('rel') || '')).length;
  return { contenu_mixte: httpRes, blank_sans_noopener: blankSansNoopener };
}
"""

JS_LIENS = "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(Boolean)"

# Détection de la pile technique (framework, CMS, trackers) : sert à adapter
# les recommandations et à dire à l'utilisateur sur QUEL type de site il est.
JS_TECH = r"""
() => {
  const meta = (n) => { const m = document.querySelector('meta[name="' + n + '"]'); return m ? m.getAttribute('content') : null; };
  const gen = meta('generator') || '';
  const html = (document.documentElement.outerHTML || '').slice(0, 250000);
  const tech = [], add = (n) => { if (!tech.includes(n)) tech.push(n); };
  // Next.js : App Router n'expose plus __NEXT_DATA__ -> on s'appuie sur /_next/static/
  if (window.__NEXT_DATA__ || document.querySelector('#__next, script[src*="/_next/"], link[href*="/_next/"]') || /\/_next\/static\//.test(html)) add('Next.js');
  if (window.__NUXT__ || document.querySelector('#__nuxt') || /\/_nuxt\//.test(html)) add('Nuxt');
  if (window.React || document.querySelector('[data-reactroot],[data-reactid]') || /_reactListening|__reactContainer\$/.test(html)) add('React');
  if (window.Vue || document.querySelector('[data-server-rendered],[data-v-app]')) add('Vue');
  if (document.querySelector('[ng-version]') || window.getAllAngularRootElements) add('Angular');
  // Svelte : classes scopées « svelte-<hash> » (le hash évite les faux positifs)
  if (/class="[^"]*\bsvelte-[a-z0-9]{5,}/.test(html) || document.querySelector('[class*="svelte-"]') && /svelte-[a-z0-9]{5,}/.test(html)) add('Svelte');
  if (document.querySelector('astro-island,[data-astro-cid]') || /\/_astro\//.test(html)) add('Astro');
  if (window.Shopify || /cdn\.shopify\.com/.test(html)) add('Shopify');
  if (/wp-content|wp-includes|wp-json/.test(html) || /wordpress/i.test(gen)) add('WordPress');
  if (/wixstatic\.com|_wixCssState|X-Wix/i.test(html)) add('Wix');
  if (/squarespace/i.test(gen) || /static1\.squarespace\.com|squarespace-cdn\.com/.test(html)) add('Squarespace');
  // Webflow : attribut data-wf-* sur <html> (le mot "webflow" dans le HTML est trop ambigu)
  if (document.querySelector('html[data-wf-page], [data-wf-domain], [data-wf-site]') || /webflow/i.test(gen)) add('Webflow');
  if (/Drupal\.settings|\/sites\/default\/files/.test(html) || /drupal/i.test(gen)) add('Drupal');
  if (/joomla/i.test(gen)) add('Joomla');
  if (window.jQuery || (window.$ && window.$.fn && window.$.fn.jquery)) add('jQuery');
  if (/elementor/i.test(html)) add('Elementor');
  if (/bootstrap/i.test(html) && document.querySelector('.container,.row,.col,[class*="col-"]')) add('Bootstrap');
  if (document.querySelector('[class*="css-"]') && /tailwind|--tw-/.test(html)) add('Tailwind CSS');
  const trackers = [], tk = (c, n) => { if (c) trackers.push(n); };
  tk(window.gtag || window.dataLayer || /googletagmanager\.com|google-analytics\.com/.test(html), 'Google Analytics / GTM');
  tk(window.fbq || /connect\.facebook\.net/.test(html), 'Meta Pixel');
  tk(window.hj || /static\.hotjar\.com/.test(html), 'Hotjar');
  tk(/plausible\.io/.test(html), 'Plausible');
  tk(/matomo|piwik/.test(html), 'Matomo');
  tk(/clarity\.ms/.test(html), 'Microsoft Clarity');
  tk(/js\.stripe\.com/.test(html), 'Stripe');
  return {
    tech, generator: gen || null, trackers,
    spa: !!(window.__NEXT_DATA__ || window.__NUXT__ || document.querySelector('#__next,#__nuxt,[ng-version],[data-server-rendered]')),
  };
}
"""

# Analyse des images : poids inutile (sur-dimensionnées), CLS (dimensions
# manquantes), images cassées, lazy-loading non utilisé hors écran.
JS_IMAGES = r"""
() => {
  const vh = window.innerHeight, dpr = window.devicePixelRatio || 1;
  const imgs = Array.from(document.images);
  let surdim = 0, sansDim = 0, cassees = 0, sansLazy = 0;
  const exemples = [];
  for (const img of imgs) {
    const r = img.getBoundingClientRect();
    const visible = r.width > 1 && r.height > 1;
    if (img.complete && img.naturalWidth === 0 && (img.currentSrc || img.src)) { cassees++; continue; }
    const cs = getComputedStyle(img);
    const aDim = (img.getAttribute('width') && img.getAttribute('height')) || cs.aspectRatio !== 'auto';
    if (visible && !aDim) sansDim++;
    if (visible && img.naturalWidth > 0) {
      const trop = img.naturalWidth / Math.max(1, r.width * dpr);
      // seuil 2.5 : laisse passer le 2× rétina légitime, ne signale que le vrai gâchis
      if (trop > 2.5 && r.width * dpr > 64) {
        surdim++;
        if (exemples.length < 6) exemples.push({
          src: (img.currentSrc || img.src || '').split('/').pop().split('?')[0].slice(0, 42),
          naturel: img.naturalWidth + '×' + img.naturalHeight, affiche: Math.round(r.width) + 'px',
        });
      }
    }
    if (r.top > vh && visible && img.loading !== 'lazy') sansLazy++;
  }
  return { total: imgs.length, surdimensionnees: surdim, sans_dimensions: sansDim, cassees, sans_lazy: sansLazy, exemples };
}
"""

# Sondes "tête de page" : render-blocking, i18n (hreflang), PWA, zoom bloqué,
# contenu de remplissage (lorem ipsum, "coming soon"…), polices d'icônes nues.
JS_HEAD = r"""
() => {
  const cssBloquant = Array.from(document.querySelectorAll('head link[rel="stylesheet"][href]'))
    .filter(l => { const m = (l.getAttribute('media') || '').trim().toLowerCase(); return !m || m === 'all' || m === 'screen'; }).length;
  const jsBloquant = Array.from(document.querySelectorAll('head script[src]'))
    .filter(s => !s.async && !s.defer && (s.getAttribute('type') || '').toLowerCase() !== 'module').length;
  const hreflang = Array.from(document.querySelectorAll('link[rel="alternate"][hreflang]'))
    .map(l => l.getAttribute('hreflang')).filter(Boolean);
  const vp = (document.querySelector('meta[name="viewport"]') || {}).content || '';
  const zoomBloque = /user-scalable\s*=\s*(no|0)/i.test(vp) || /maximum-scale\s*=\s*(1|0?\.9)/i.test(vp);
  const txt = ((document.body ? document.body.innerText : '') || '').toLowerCase();
  const placeholders = [];
  ['lorem ipsum', 'coming soon', 'bientôt disponible', 'page en construction', 'under construction',
   'votre texte ici', 'insérez votre', 'dummy text', 'placeholder text'].forEach(p => { if (txt.includes(p)) placeholders.push(p); });
  return {
    css_bloquant: cssBloquant, js_bloquant: jsBloquant, hreflang,
    manifest: !!document.querySelector('link[rel="manifest"]'),
    theme_color: !!document.querySelector('meta[name="theme-color"]'),
    apple_icon: !!document.querySelector('link[rel="apple-touch-icon"]'),
    zoom_bloque: zoomBloque, placeholders,
  };
}
"""

# Ergonomie tactile : cibles trop petites (<24px) et polices minuscules (<12px).
# À exécuter SOUS viewport mobile (sinon les seuils n'ont pas de sens).
JS_TACTILE = r"""
() => {
  const cibles = Array.from(document.querySelectorAll(
    'a[href], button, [role="button"], input:not([type="hidden"]), select, textarea, [onclick]'));
  let petites = 0; const exemples = [];
  for (const el of cibles) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (r.width < 24 || r.height < 24) {
      petites++;
      if (exemples.length < 6) {
        let id = el.tagName.toLowerCase();
        if (el.id) id += '#' + el.id;
        else if (typeof el.className === 'string' && el.className.trim()) id += '.' + el.className.trim().split(/\s+/)[0];
        exemples.push({ el: id.slice(0, 44), w: Math.round(r.width), h: Math.round(r.height) });
      }
    }
  }
  let minuscules = 0;
  for (const n of Array.from(document.querySelectorAll('p,li,a,span,td,label,small,div')).slice(0, 1800)) {
    const t = (n.childNodes.length && n.firstChild && n.firstChild.nodeType === 3) ? (n.textContent || '').trim() : '';
    if (!t) continue;
    const fs = parseFloat(getComputedStyle(n).fontSize);
    if (fs && fs < 12) minuscules++;
  }
  return { cibles_petites: petites, exemples_petits: exemples, polices_minuscules: minuscules };
}
"""

# Sélecteurs de boutons de consentement (bannières cookies) à cliquer pour
# auditer le vrai site et non la modale. Ordre = priorité (accepter > fermer).
JS_CONSENTEMENT = r"""
() => {
  // FORTS : phrases quasi exclusives aux bannières cookies -> cliquables même sans
  // contexte. GÉNÉRIQUES : mots ambigus -> uniquement dans un contexte cookie avéré
  // (sinon on risque de cliquer un « OK »/« Continuer » qui fait quitter la page).
  const forts = ['tout accepter', 'accepter tout', 'tout autoriser', 'accepter les cookies',
    "j'accepte", 'accept all', 'allow all', 'accept cookies', 'accept & close', 'autoriser tous'];
  const generiques = ['accepter', "j'ai compris", 'i agree', 'i accept', 'got it', 'agree',
    'continuer', 'continue', 'ok', 'compris', 'consent', 'fermer'];
  const cands = Array.from(document.querySelectorAll(
    'button, a[role="button"], [role="button"], a, input[type="button"], input[type="submit"]'));
  const dansContexte = (el) => {
    let p = el, prof = 0;
    while (p && prof < 6) {
      const t = ((p.id || '') + ' ' + (typeof p.className === 'string' ? p.className : '')
        + ' ' + (p.getAttribute && (p.getAttribute('aria-label') || '') || '')).toLowerCase();
      if (/cookie|consent|gdpr|rgpd|cmp|privacy|confidentialit/.test(t)) return true;
      p = p.parentElement; prof++;
    }
    return false;
  };
  let meilleur = null, score = -1;
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const txt = ((el.textContent || el.value || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().toLowerCase();
    if (!txt || txt.length > 40) continue;
    let s = -1;
    for (let i = 0; i < forts.length; i++) { if (txt.includes(forts[i])) { s = 200 - i; break; } }
    if (s < 0 && dansContexte(el)) {
      for (let i = 0; i < generiques.length; i++) { if (txt.includes(generiques[i])) { s = 100 - i; break; } }
    }
    if (s > score) { score = s; meilleur = el; }
  }
  if (meilleur && score > 0) {
    meilleur.setAttribute('data-auditeur-consent', '1');
    return { trouve: true, texte: (meilleur.textContent || '').trim().slice(0, 40) };
  }
  return { trouve: false };
}
"""

# Métadonnées d'un champ de formulaire (sert à choisir la bonne valeur du profil).
JS_INFOS_CHAMP = r"""el => {
  let lab = '';
  try { if (el.labels && el.labels[0]) lab = el.labels[0].textContent || ''; } catch (e) {}
  if (!lab) lab = el.getAttribute('aria-label') || '';
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || 'text').toLowerCase(),
    name: el.getAttribute('name') || '',
    id: el.id || '',
    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    placeholder: el.getAttribute('placeholder') || '',
    label: (lab || '').trim().slice(0, 80),
    required: !!(el.required || el.getAttribute('aria-required') === 'true'),
  };
}"""

# Messages d'erreur visibles après soumission (heuristique multi-frameworks).
JS_ERREURS_FORM = r"""() => {
  const sel = '[role="alert"], [aria-invalid="true"], .error, .errors, .invalid-feedback,'
    + ' .text-red-500, .text-danger, .help-block, .form-error, .field-error';
  const txt = Array.from(document.querySelectorAll(sel))
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  return Array.from(new Set(txt)).slice(0, 6);
}"""


def _valeur_champ(infos: dict, profil: dict, etat: dict):
    """Choisit une valeur du profil pour un champ texte. etat['mdp'] mémorise le 1er mot de passe."""
    typ = infos.get("type", "text")
    ac = infos.get("autocomplete", "")
    foin = " ".join([infos.get("name", ""), infos.get("id", ""), infos.get("placeholder", ""),
                     infos.get("label", ""), ac]).lower()

    def a(*mots):
        return any(m in foin for m in mots)

    g = profil.get

    # mot de passe (et sa confirmation)
    if typ == "password" or ac in ("new-password", "current-password") or a("password", "mot de passe", "mdp", "passe"):
        mdp = g("mot_de_passe") or "Test1234!"
        if a("confirm", "confirmation", "répét", "repet", "again", "vérif", "verif", "retape"):
            return etat.get("mdp", mdp)
        etat["mdp"] = mdp
        return mdp
    if typ == "email" or ac == "email" or a("email", "e-mail", "mail", "courriel"):
        return g("email") or "test@example.com"
    if typ == "tel" or ac in ("tel", "tel-national") or a("phone", "téléphone", "telephone", "portable", "mobile", "whatsapp", "numéro"):
        return g("telephone") or "+33600000000"
    if ac == "given-name" or a("prénom", "prenom", "first-name", "firstname", "first_name", "given"):
        return g("prenom")
    if ac == "family-name" or a("last-name", "lastname", "last_name", "surname", "family", "nom de famille"):
        return g("nom")
    if ac == "username" or a("username", "user-name", "identifiant", "pseudo", "nom d'utilisateur", "login"):
        return g("identifiant") or g("email")
    if ac == "name" or a("nom complet", "full-name", "fullname", "full_name", "votre nom", "name", "nom"):
        return g("nom_complet") or (f"{g('prenom', '')} {g('nom', '')}".strip() or g("nom"))
    if ac == "organization" or a("organization", "entreprise", "société", "societe", "company", "compagnie"):
        return g("entreprise")
    if ac in ("street-address", "address-line1") or a("adresse", "address", "rue", "street"):
        return g("adresse")
    if ac == "postal-code" or a("code postal", "postal", "zip", "cp"):
        return g("code_postal")
    if ac == "address-level2" or a("ville", "city", "commune"):
        return g("ville")
    if ac == "country-name" or a("pays", "country"):
        return g("pays")
    if a("sujet", "subject", "objet"):
        return g("sujet") or "Test automatique"
    if infos.get("tag") == "textarea" or a("message", "comment", "commentaire", "votre demande", "description"):
        return g("message") or "Ceci est un test automatique."

    for cle, val in (profil.get("champs_personnalises") or {}).items():
        if cle.lower() in foin:
            return val

    if typ == "url":
        return "https://example.com"
    if typ == "number":
        return "1"
    if typ == "date":
        return "2000-01-01"
    if typ in ("text", "search"):
        return g("nom_complet") or "Test"
    return None


# ============================================================
#  CONFIGURATION
# ============================================================

@dataclass
class Config:
    url: str
    domaine: str = ""
    sous_domaines: bool = False
    max_pages: int = 30
    max_depth: int = 3
    concurrence: int = 4
    timeout: int = 20000          # ms par navigation
    settle_ms: int = 1200         # attente pour stabiliser LCP/CLS
    respecter_robots: bool = True
    soumettre_formulaires: bool = False
    autoriser_prod: bool = False  # autorise la soumission réelle hors site de test
    profil: dict = None           # infos perso pour remplir les formulaires
    scenario: dict = None         # parcours multi-étapes à rejouer (mode scénario)
    scenarios: list = None        # plusieurs parcours à rejouer (mode combiné)
    faire_audit: bool = True      # exécuter aussi l'audit générique (crawl)
    auto: bool = False            # explorer automatiquement les tunnels (sans scénario)
    auto_max_etapes: int = 14     # nombre max d'écrans explorés en mode auto
    headful: bool = False
    mobile: bool = False
    lent: bool = False            # bride la connexion (simule un mobile bas débit type 3G)
    gerer_cookies: bool = True    # ferme automatiquement les bannières de consentement
    max_liens: int = 250          # plafond de liens vérifiés (HTTP)
    inclure: object = None        # motif regex compilé (re.Pattern) ou None
    exclure: object = None        # motif regex compilé (re.Pattern) ou None
    storage_state: str | None = None
    dossier: Path = field(default_factory=lambda: Path("."))
    familles: set = field(default_factory=lambda: set(CATEGORIES + ["liens", "formulaires"]))
    client: bool = False          # génère AUSSI rapport-client.html (diagnostic commercial)
    prestataire: dict = None      # coordonnées affichées dans le rapport client


# ============================================================
#  HELPERS URL
# ============================================================

def normaliser_url(url: str) -> str:
    """Retire le fragment, met l'hôte en minuscules, normalise le port et le slash final."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    netloc = p.netloc.lower()
    if p.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif p.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((p.scheme, netloc, path, "", p.query, ""))


def hote(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def meme_site(url: str, base_host: str, sous_domaines: bool) -> bool:
    h = hote(url)
    if not h:
        return False
    if sous_domaines:
        return h == base_host or h.endswith("." + base_host)
    return h == base_host


def est_html_probable(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    ext = Path(p.path).suffix.lower()
    return ext == "" or ext in (".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".xhtml")


def slug_fichier(url: str) -> str:
    p = urlparse(url)
    base = (p.path or "/").strip("/").replace("/", "_") or "accueil"
    base = re.sub(r"[^A-Za-z0-9_-]", "-", base)[:50]
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{base or 'page'}_{h}"


# ============================================================
#  AUDITEUR
# ============================================================

class Auditeur:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.visites: set[str] = set()
        self.enfile: set[str] = set()
        self.resultats: list[dict] = []
        self.tous_liens: dict[str, set[str]] = {}   # cible -> pages sources
        self.statut_lien: dict[str, object] = {}     # url -> code / "erreur"
        self.axe_source: str | None = None
        self.robots: RobotFileParser | None = None
        self.file: asyncio.Queue | None = None
        self.infos_site: dict = {}                    # stack, redirection HTTPS, soft-404, sitemap…
        self.entetes_ressources: dict[str, dict] = {}  # origine -> {compresse, sans_cache, total}

    # ---- axe-core : embarqué via le paquet python, sinon CDN ----
    async def charger_axe(self, contexte) -> None:
        if "accessibilite" not in self.cfg.familles:
            return
        try:
            import axe_playwright_python
            chemin = Path(axe_playwright_python.__file__).parent / "axe.min.js"
            if chemin.exists():
                self.axe_source = chemin.read_text(encoding="utf-8")
        except Exception:
            self.axe_source = None
        if not self.axe_source:
            try:
                rep = await contexte.request.get(AXE_CDN, timeout=10000)
                if rep.ok:
                    self.axe_source = await rep.text()
            except Exception:
                self.axe_source = None
        if not self.axe_source:
            print("⚠️  axe-core indisponible : seuls les checks d'accessibilité manuels seront faits.")

    # ---- robots.txt ----
    async def charger_robots(self, contexte) -> None:
        if not self.cfg.respecter_robots:
            return
        try:
            base = f"{urlparse(self.cfg.url).scheme}://{urlparse(self.cfg.url).netloc}"
            rep = await contexte.request.get(urljoin(base, "/robots.txt"), timeout=10000)
            if rep.ok:
                rp = RobotFileParser()
                rp.parse((await rep.text()).splitlines())
                self.robots = rp
        except Exception:
            self.robots = None

    def robots_ok(self, url: str) -> bool:
        if not self.cfg.respecter_robots or self.robots is None:
            return True
        try:
            return self.robots.can_fetch("*", url)
        except Exception:
            return True

    def lien_autorise(self, url: str) -> bool:
        """Filtre inclure/exclure (motifs regex déjà compilés) appliqué au crawl."""
        if self.cfg.exclure and self.cfg.exclure.search(url):
            return False
        if self.cfg.inclure and not self.cfg.inclure.search(url):
            return False
        return True

    # ---- bridage réseau (simule un mobile bas débit, ~ "Slow 3G") ----
    async def _brider(self, page):
        if not self.cfg.lent:
            return
        try:
            client = await page.context.new_cdp_session(page)
            # Network.enable est requis pour que emulateNetworkConditions s'applique.
            await client.send("Network.enable")
            # cache vidé : on mesure la pire expérience (1re visite en bas débit)
            await client.send("Network.setCacheDisabled", {"cacheDisabled": True})
            await client.send("Network.emulateNetworkConditions", {
                "offline": False,
                "downloadThroughput": int(400 * 1024 / 8),   # ~400 kbps
                "uploadThroughput": int(400 * 1024 / 8),
                "latency": 400,                                # 400 ms RTT
            })
            await client.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        except Exception:
            pass

    # ---- analyse au niveau du site (une seule fois) ----
    async def analyser_site(self, contexte) -> list[str]:
        """Renvoie une liste d'URLs (sitemap) à amorcer ; remplit self.infos_site."""
        info: dict = {}
        base = f"{urlparse(self.cfg.url).scheme}://{urlparse(self.cfg.url).netloc}"
        racine = f"https://{self.cfg.domaine}"

        # 1) Redirection HTTP -> HTTPS (sécurité) : on demande la version http://.
        try:
            rep = await contexte.request.get(
                f"http://{self.cfg.domaine}/", timeout=10000, max_redirects=0)
            loc = rep.headers.get("location", "")
            info["http_redirige_https"] = (300 <= rep.status < 400 and loc.startswith("https://")) or rep.status == 0
        except Exception:
            # une exception de redirection signifie souvent qu'une redirection a eu lieu
            info["http_redirige_https"] = None

        # 2) Canonicalisation www / apex : les deux doivent mener au même endroit.
        try:
            alt = self.cfg.domaine[4:] if self.cfg.domaine.startswith("www.") else "www." + self.cfg.domaine
            rep = await contexte.request.get(f"https://{alt}/", timeout=10000, max_redirects=5)
            dest = hote(rep.url)
            info["www_canonique"] = (dest == self.cfg.domaine) or (rep.status >= 400)
            info["www_alt"] = alt
        except Exception:
            info["www_canonique"] = None

        # 3) Soft-404 : une URL bidon doit renvoyer un vrai 404, pas un 200.
        try:
            bidon = urljoin(base, "/auditeur-page-inexistante-" + hashlib.sha1(base.encode()).hexdigest()[:10])
            rep = await contexte.request.get(bidon, timeout=10000, max_redirects=3)
            info["soft_404"] = (rep.status == 200)
            info["code_404_teste"] = rep.status
        except Exception:
            info["soft_404"] = None

        # 4) Sitemap : robots.txt -> Sitemap:, sinon /sitemap.xml. On déplie les index.
        graines: list[str] = []
        urls_sitemap = []
        try:
            rep = await contexte.request.get(urljoin(base, "/robots.txt"), timeout=8000)
            if rep.ok:
                for ligne in (await rep.text()).splitlines():
                    if ligne.lower().startswith("sitemap:"):
                        urls_sitemap.append(ligne.split(":", 1)[1].strip())
        except Exception:
            pass
        # les emplacements standards restent testés même si robots.txt déclare un
        # sitemap injoignable (autre domaine, prod depuis un audit localhost…)
        urls_sitemap += [urljoin(base, "/sitemap.xml"), urljoin(racine, "/sitemap_index.xml")]
        info["sitemap"] = False
        vus_sm = set()
        for sm in urls_sitemap[:4]:
            if sm in vus_sm:
                continue
            vus_sm.add(sm)
            try:
                rep = await contexte.request.get(sm, timeout=10000)
                if not rep.ok:
                    continue
                corps = await rep.text()
                info["sitemap"] = True
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", corps)
                # index de sitemaps -> on déplie un seul sous-sitemap
                if "<sitemapindex" in corps and locs:
                    try:
                        rep2 = await contexte.request.get(locs[0], timeout=10000)
                        if rep2.ok:
                            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", await rep2.text())
                    except Exception:
                        pass
                graines.extend(locs)
            except Exception:
                continue

        self.infos_site = info
        return graines[: self.cfg.max_pages]

    # ---- boucle principale ----
    async def lancer(self):
        async with async_playwright() as p:
            try:
                navigateur = await p.chromium.launch(headless=not self.cfg.headful)
            except Exception as e:
                sys.exit("❌ Chromium introuvable ou échec du lancement. Lance :  "
                         "playwright install chromium\n   (" + str(e).splitlines()[0] + ")")
            vp = VIEWPORTS["mobile"] if self.cfg.mobile else VIEWPORTS["bureau"]
            contexte = await navigateur.new_context(
                viewport=vp,
                ignore_https_errors=True,
                is_mobile=self.cfg.mobile,
                storage_state=self.cfg.storage_state,
            )
            contexte.set_default_timeout(self.cfg.timeout)

            await self.charger_axe(contexte)
            await self.charger_robots(contexte)
            await contexte.add_init_script(script=JS_VITALS)
            if self.axe_source:
                await contexte.add_init_script(script=self.axe_source)

            # Analyse au niveau du SITE (une fois) : redirection HTTPS, www,
            # soft-404, sitemap. Le sitemap sert ensuite à amorcer le crawl.
            graines = await self.analyser_site(contexte)

            self.file = asyncio.Queue()
            depart = normaliser_url(self.cfg.url)
            self.enfile.add(depart)
            await self.file.put((self.cfg.url, 0))
            # amorce par le sitemap : on découvre des pages que le crawl par liens
            # raterait (typique des SPA et des gros sites).
            for g in graines:
                ng = normaliser_url(g)
                if (ng not in self.enfile and len(self.enfile) < self.cfg.max_pages
                        and meme_site(g, self.cfg.domaine, self.cfg.sous_domaines)
                        and est_html_probable(g) and self.lien_autorise(g)):
                    self.enfile.add(ng)
                    await self.file.put((g, 1))

            travailleurs = [
                asyncio.create_task(self._travailleur(contexte))
                for _ in range(self.cfg.concurrence)
            ]
            await self.file.join()
            for t in travailleurs:
                t.cancel()
            await asyncio.gather(*travailleurs, return_exceptions=True)

            if "liens" in self.cfg.familles:
                await self.verifier_liens(contexte)

            await contexte.close()
            await navigateur.close()

    async def _travailleur(self, contexte):
        while True:
            url, prof = await self.file.get()
            try:
                # Tout le corps est protégé : un worker ne doit JAMAIS mourir sur
                # une page/un lien problématique, sinon file.join() bloque pour toujours.
                nu = normaliser_url(url)
                if nu in self.visites or len(self.visites) >= self.cfg.max_pages:
                    continue
                self.visites.add(nu)
                print(f"🔍 [{len(self.visites)}/{self.cfg.max_pages}] {url}")
                try:
                    res = await self.auditer_page(contexte, url, prof)
                except Exception as e:
                    res = {"url": url, "statut": f"erreur: {e}", "problemes": [
                        {"categorie": "fiabilite", "severite": MAJEUR, "message": f"Échec de l'audit : {e}"}]}
                self.resultats.append(res)

                # déduplique aussi l'URL finale après redirection (évite le double audit)
                nu_finale = normaliser_url(res.get("url_finale") or url)
                self.visites.add(nu_finale)
                self.enfile.add(nu_finale)

                # enfiler les liens internes
                if prof < self.cfg.max_depth:
                    for lien in res.get("_liens_internes", []):
                        nlu = normaliser_url(lien)
                        if nlu in self.enfile or len(self.enfile) >= self.cfg.max_pages * 5:
                            continue
                        if (meme_site(lien, self.cfg.domaine, self.cfg.sous_domaines)
                                and est_html_probable(lien)
                                and self.robots_ok(lien)
                                and self.lien_autorise(lien)):
                            self.enfile.add(nlu)
                            await self.file.put((lien, prof + 1))
            except Exception as e:
                print(f"   ⚠️  worker : erreur ignorée sur {url} ({e})")
            finally:
                self.file.task_done()

    # ---- audit d'une page ----
    async def auditer_page(self, contexte, url: str, prof: int) -> dict:
        page = await contexte.new_page()
        await self._brider(page)
        erreurs_console: list[str] = []
        erreurs_js: list[str] = []
        requetes_echouees: list[str] = []
        # compression / cache des ressources textuelles servies par le site
        livraison = {"total": 0, "non_compresse": [], "sans_cache": []}

        def _capter_livraison(r):
            try:
                rt = r.request.resource_type
                if rt not in ("script", "stylesheet", "document", "fetch", "xhr", "font"):
                    return
                if not meme_site(r.url, self.cfg.domaine, True):
                    return
                h = r.headers
                ce = (h.get("content-encoding", "")).lower()
                cc = (h.get("cache-control", "")).lower()
                taille = 0
                try:
                    taille = int(h.get("content-length", "0"))
                except Exception:
                    taille = 0
                livraison["total"] += 1
                # >2 ko non compressé = gâchis (le petit reste sous le seuil de compression)
                if not ce and taille > 2048 and rt in ("script", "stylesheet", "document"):
                    if len(livraison["non_compresse"]) < 8:
                        livraison["non_compresse"].append(r.url.split("/")[-1].split("?")[0][:50])
                # actif statique sans cache durable
                if rt in ("script", "stylesheet", "font") and ("no-store" in cc or "no-cache" in cc or not cc):
                    if len(livraison["sans_cache"]) < 8:
                        livraison["sans_cache"].append(r.url.split("/")[-1].split("?")[0][:50])
            except Exception:
                pass

        page.on("console", lambda m: erreurs_console.append(f"{m.text}"[:300]) if m.type == "error" else None)
        page.on("pageerror", lambda e: erreurs_js.append(f"{e}"[:300]))
        page.on("requestfailed", lambda r: requetes_echouees.append(
            f"{r.url[:120]} ({r.failure})") if not r.url.startswith("data:") else None)
        page.on("response", lambda r: requetes_echouees.append(
            f"{r.status} {r.url[:120]}") if r.status >= 400 else None)
        page.on("response", _capter_livraison)

        res: dict = {"url": url, "profondeur": prof}
        try:
            reponse = await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeout)
            res["statut"] = reponse.status if reponse else None
            res["url_finale"] = page.url
            entetes = reponse.headers if reponse else {}
        except Exception as e:
            await page.close()
            return {"url": url, "statut": f"erreur navigation: {e}", "problemes": [
                {"categorie": "fiabilite", "severite": CRITIQUE, "message": f"Page inaccessible : {e}"}]}

        for etat, to in (("load", self.cfg.timeout), ("networkidle", 6000)):
            try:
                await page.wait_for_load_state(etat, timeout=to)
            except Exception:
                pass
        await page.wait_for_timeout(self.cfg.settle_ms)

        # Ferme la bannière de consentement (cookies) pour auditer le vrai site
        # et non la modale ; on note qu'elle existait.
        res["banniere_cookies"] = await self._fermer_consentement(page)

        res["titre"] = await page.title()
        tests: dict = {}

        # Détection de la pile technique (une fois, depuis la 1re page = souvent l'accueil).
        infos_tech = await self._safe(page.evaluate, JS_TECH, defaut={})
        if infos_tech and not self.infos_site.get("tech"):
            self.infos_site["tech"] = infos_tech.get("tech")
            self.infos_site["generator"] = infos_tech.get("generator")
            self.infos_site["trackers"] = infos_tech.get("trackers")
            self.infos_site["spa"] = infos_tech.get("spa")

        if "performance" in self.cfg.familles:
            perf = await self._safe(page.evaluate, JS_PERF, defaut={})
            perf["images"] = await self._safe(page.evaluate, JS_IMAGES, defaut={})
            perf["head"] = await self._safe(page.evaluate, JS_HEAD, defaut={})
            perf["livraison"] = {
                "total": livraison["total"],
                "non_compresse": list(dict.fromkeys(livraison["non_compresse"])),
                "sans_cache": list(dict.fromkeys(livraison["sans_cache"])),
            }
            tests["performance"] = perf

        if "seo" in self.cfg.familles:
            seo = await self._safe(page.evaluate, JS_SEO, defaut={})
            # hreflang/i18n récupéré via la sonde tête de page si la perf n'a pas tourné
            if "performance" not in self.cfg.familles:
                seo["_head"] = await self._safe(page.evaluate, JS_HEAD, defaut={})
            tests["seo"] = seo

        if "accessibilite" in self.cfg.familles:
            tests["accessibilite"] = await self.analyser_accessibilite(page)

        if "securite" in self.cfg.familles:
            tests["securite"] = await self.analyser_securite(page, contexte, entetes, url)

        # liens (collecte pour le crawl + vérification globale)
        liens = await self._safe(page.evaluate, JS_LIENS, defaut=[])
        internes, externes = [], []
        for l in liens:
            try:
                nl = normaliser_url(l)
            except Exception:
                continue
            if urlparse(nl).scheme not in ("http", "https"):
                continue
            self.tous_liens.setdefault(nl, set()).add(url)
            (internes if meme_site(nl, self.cfg.domaine, self.cfg.sous_domaines) else externes).append(nl)
        res["_liens_internes"] = internes
        tests["liens"] = {"internes": len(set(internes)), "externes": len(set(externes))}

        # On fige les erreurs MAINTENANT, avant que le responsive ne change le
        # viewport (un resize peut relancer des requêtes et fausser ces compteurs).
        res["erreurs_console"] = erreurs_console[:25]
        res["erreurs_js"] = erreurs_js[:25]
        res["requetes_echouees"] = list(dict.fromkeys(requetes_echouees))[:25]

        # responsive EN DERNIER (modifie le viewport)
        if "responsive" in self.cfg.familles:
            tests["responsive"] = await self.analyser_responsive(page)

        res["tests"] = tests

        # capture d'écran
        chemin = self.cfg.dossier / "captures" / f"{slug_fichier(url)}.png"
        try:
            await page.set_viewport_size(VIEWPORTS["mobile"] if self.cfg.mobile else VIEWPORTS["bureau"])
            await page.screenshot(path=str(chemin), full_page=True)
            # as_posix() -> séparateur '/' portable dans le src HTML (sinon backslash Windows)
            res["capture"] = chemin.relative_to(self.cfg.dossier).as_posix()
        except Exception:
            res["capture"] = None

        # Formulaires EN TOUT DERNIER : remplir/soumettre peut modifier le DOM ou
        # faire naviguer la page (création de compte) → ne doit rien fausser avant.
        if "formulaires" in self.cfg.familles:
            tests["formulaires"] = await self.analyser_formulaires(page)

        await page.close()
        res["problemes"] = detecter_problemes(res)
        return res

    async def _safe(self, fn, *a, defaut=None, **k):
        try:
            return await fn(*a, **k)
        except Exception:
            return defaut

    async def analyser_accessibilite(self, page) -> dict:
        out = await self._safe(page.evaluate, JS_A11Y_MANUEL, defaut={})
        if self.axe_source:
            axe = await self._safe(
                page.evaluate,
                "async () => (typeof axe !== 'undefined') "
                "? await axe.run(document, {resultTypes:['violations']}) : null",
                defaut=None,
            )
            if axe and isinstance(axe, dict):
                out["axe_violations"] = [{
                    "id": v["id"],
                    "impact": v.get("impact") or "minor",
                    "description": v["description"],
                    "aide": v.get("help"),
                    "noeuds": len(v.get("nodes", [])),
                    # sélecteurs des premiers éléments fautifs : indispensable pour corriger
                    "cibles": [", ".join(n.get("target", []))
                               for n in v.get("nodes", [])[:4]],
                } for v in axe.get("violations", [])]
            else:
                out["axe_violations"] = None
        else:
            out["axe_violations"] = None
        return out

    async def analyser_securite(self, page, contexte, entetes: dict, url: str) -> dict:
        e = {k.lower(): v for k, v in (entetes or {}).items()}
        dom = await self._safe(page.evaluate, JS_SECU_DOM, defaut={"contenu_mixte": 0, "blank_sans_noopener": 0})
        # cookies non sécurisés
        cookies_faibles = []
        try:
            for c in await contexte.cookies():
                manques = []
                if not c.get("secure"):
                    manques.append("Secure")
                if not c.get("httpOnly"):
                    manques.append("HttpOnly")
                if not c.get("sameSite") or c.get("sameSite") == "None":
                    manques.append("SameSite")
                if manques:
                    cookies_faibles.append({"nom": c.get("name"), "manque": manques})
        except Exception:
            pass
        return {
            "https": urlparse(url).scheme == "https",
            "strict_transport_security": e.get("strict-transport-security", "absent"),
            "content_security_policy": "présent" if e.get("content-security-policy") else "absent",
            "csp_unsafe": bool(e.get("content-security-policy")
                               and re.search(r"unsafe-(inline|eval)", e["content-security-policy"])),
            "x_frame_options": e.get("x-frame-options", "absent"),
            "x_content_type_options": e.get("x-content-type-options", "absent"),
            "referrer_policy": e.get("referrer-policy", "absent"),
            "permissions_policy": e.get("permissions-policy", "absent"),
            "serveur_divulgue": e.get("server") or e.get("x-powered-by") or None,
            "contenu_mixte": dom.get("contenu_mixte", 0),
            "blank_sans_noopener": dom.get("blank_sans_noopener", 0),
            "cookies_faibles": cookies_faibles[:10],
        }

    async def _fermer_consentement(self, page) -> dict:
        """Repère et clique le bouton « accepter » d'une bannière cookies.
        Renvoie {trouve, ferme, texte} ; ne bloque jamais l'audit en cas d'échec."""
        if not self.cfg.gerer_cookies:
            return {"trouve": False, "ferme": False}
        info = await self._safe(page.evaluate, JS_CONSENTEMENT, defaut={"trouve": False})
        if not info or not info.get("trouve"):
            return {"trouve": False, "ferme": False}
        ferme = False
        try:
            await page.locator("[data-auditeur-consent='1']").first.click(timeout=2500)
            await page.wait_for_timeout(500)
            ferme = True
        except Exception:
            pass
        return {"trouve": True, "ferme": ferme, "texte": info.get("texte")}

    async def analyser_responsive(self, page) -> dict:
        out = {}
        for nom, taille in VIEWPORTS.items():
            try:
                await page.set_viewport_size(taille)
                await page.wait_for_timeout(250)
                data = await page.evaluate(JS_OVERFLOW)
                # ergonomie tactile mesurée au viewport mobile (seuils pertinents là).
                if nom == "mobile":
                    data["tactile"] = await self._safe(page.evaluate, JS_TACTILE, defaut={})
                out[nom] = {"largeur": taille["width"], **data}
            except Exception as ex:
                out[nom] = {"largeur": taille["width"], "erreur": str(ex)}
        return out

    def _submit_autorise(self):
        """Garde-fou : la soumission réelle n'est permise que si explicitement demandée,
        et hors prod uniquement avec --autoriser-prod (pour ne pas polluer un vrai site)."""
        if not self.cfg.soumettre_formulaires:
            return False, "non soumis (ajoute --soumettre-formulaires)"
        h = hote(self.cfg.url)
        est_test = (h in ("localhost", "127.0.0.1", "0.0.0.0") or h.endswith(".local")
                    or any(m in h for m in ("staging", "preview", "sandbox", "dev.", "-dev", ".test")))
        if est_test or self.cfg.autoriser_prod:
            return True, "ok"
        return False, f"bloqué sur prod ({h}) — ajoute --autoriser-prod pour soumettre réellement"

    async def _remplir_select(self, sel, infos, profil):
        try:
            options = await sel.evaluate(
                "el => Array.from(el.options).map(o => ({v:o.value, t:(o.textContent||'').trim(), d:o.disabled}))")
        except Exception:
            return None
        foin = " ".join([infos.get("name", ""), infos.get("id", ""), infos.get("label", "")]).lower()
        pref = None
        if "pays" in foin or "country" in foin:
            pref = (profil or {}).get("pays")
        elif "ville" in foin or "city" in foin:
            pref = (profil or {}).get("ville")
        cible = None
        if pref:
            for o in options:
                if pref.lower() in o["t"].lower() and not o["d"]:
                    cible = o["v"]
                    break
        if cible is None:
            for o in options:
                if o["v"] and not o["d"]:
                    cible = o["v"]
                    break
        if cible is not None:
            await self._safe(sel.select_option, cible)
        return cible

    async def _type_form(self, form):
        try:
            blob = (await form.evaluate(
                "f => { const t=(f.innerText||''); const b=Array.from(f.querySelectorAll("
                "'button, input[type=submit]')).map(x=>x.innerText||x.value||'').join(' ');"
                " return (t+' '+b).toLowerCase().slice(0,2000); }"))
        except Exception:
            return "autre"
        if any(k in blob for k in ("inscription", "créer un compte", "creer un compte", "s'inscrire", "register", "sign up", "signup")):
            return "inscription"
        if any(k in blob for k in ("connexion", "se connecter", "login", "sign in", "log in")):
            return "connexion"
        if any(k in blob for k in ("recherche", "rechercher", "search")):
            return "recherche"
        if any(k in blob for k in ("contact", "message", "envoyer")):
            return "contact"
        return "autre"

    async def analyser_formulaires(self, page) -> list:
        out = []
        profil = self.cfg.profil or {}
        try:
            forms = await page.query_selector_all("form")
        except Exception:
            return out
        autorise_submit, motif = self._submit_autorise()

        for form in forms:
            etat, champs, noms_radio = {}, {}, set()
            try:
                inputs = await form.query_selector_all("input, textarea, select")
            except Exception:
                inputs = []
            for inp in inputs:
                try:
                    infos = await inp.evaluate(JS_INFOS_CHAMP)
                except Exception:
                    continue
                typ, tag = infos.get("type", "text"), infos.get("tag", "input")
                etiq = infos.get("name") or infos.get("id") or infos.get("label") or typ
                if typ in ("hidden", "submit", "button", "reset", "image"):
                    continue
                if typ == "file":
                    champs[etiq] = "(fichier ignoré)"
                    continue
                if tag == "select":
                    v = await self._remplir_select(inp, infos, profil)
                    if v is not None:
                        champs[etiq] = v
                    continue
                if typ == "radio":
                    nm = infos.get("name", "")
                    if nm and nm in noms_radio:
                        continue
                    noms_radio.add(nm)
                    await self._safe(inp.check)
                    champs[etiq] = "sélectionné"
                    continue
                if typ == "checkbox":
                    foin = (infos.get("name", "") + infos.get("id", "") + infos.get("label", "")).lower()
                    marketing = any(m in foin for m in ("newsletter", "marketing", "promo", "offre", "abonn"))
                    consentement = any(m in foin for m in ("condition", "cgu", "cgv", "terms", "accept", "consent", "politique", "privacy", "rgpd"))
                    if infos.get("required") or (consentement and not marketing):
                        await self._safe(inp.check)
                        champs[etiq] = "coché"
                    continue
                valeur = _valeur_champ(infos, profil, etat)
                if valeur is None:
                    continue
                await self._safe(inp.fill, str(valeur))
                champs[etiq] = "••••••" if typ == "password" else str(valeur)  # on masque les mdp

            entree = {"type_probable": await self._type_form(form),
                      "champs_remplis": champs, "soumis": False}

            if not autorise_submit:
                entree["soumission"] = motif
            else:
                btn = await form.query_selector(
                    "button[type='submit'], input[type='submit'], button:not([type])")
                if not btn:
                    entree["soumission"] = "aucun bouton de soumission trouvé"
                else:
                    url_avant = page.url
                    try:
                        await btn.click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(900)
                        erreurs = await self._safe(page.evaluate, JS_ERREURS_FORM, defaut=[])
                        url_apres = page.url
                        entree.update({
                            "soumis": True,
                            "url_avant": url_avant,
                            "url_apres": url_apres,
                            "redirige": url_apres != url_avant,
                            "erreurs_affichees": erreurs,
                            "succes_probable": (url_apres != url_avant) and not erreurs,
                        })
                    except Exception as ex:
                        entree.update({"soumis": False, "erreur_soumission": str(ex)})
            out.append(entree)
            # si la soumission a navigué ailleurs, les formulaires suivants sont sur une autre page
            if entree.get("redirige"):
                break
        return out

    # ---- vérification des liens (HTTP) ----
    async def verifier_liens(self, contexte):
        # on connaît déjà le statut des pages crawlées
        for r in self.resultats:
            if isinstance(r.get("statut"), int):
                self.statut_lien[normaliser_url(r["url"])] = r["statut"]

        cibles = [u for u in self.tous_liens
                  if urlparse(u).scheme in ("http", "https") and u not in self.statut_lien]
        cibles = cibles[: self.cfg.max_liens]
        if not cibles:
            return
        total = len(cibles)
        print(f"🔗 Vérification de {total} lien(s)…")
        sem = asyncio.Semaphore(self.cfg.concurrence * 2)
        faits = [0]

        async def check(u):
            async with sem:
                self.statut_lien[u] = await self._statut_http(contexte, u)
                faits[0] += 1
                if faits[0] % 25 == 0 or faits[0] == total:
                    print(f"   … {faits[0]}/{total} liens vérifiés")

        # plafond de temps global pour ne jamais figer la fin du run sur des liens lents
        budget = max(60, total * 2)
        try:
            await asyncio.wait_for(asyncio.gather(*[check(u) for u in cibles]), timeout=budget)
        except asyncio.TimeoutError:
            for u in cibles:
                self.statut_lien.setdefault(u, "non vérifié (délai dépassé)")
            print(f"   ⏱️  Vérification des liens interrompue après {budget}s.")

    async def _statut_http(self, contexte, url: str):
        for methode in ("head", "get"):
            try:
                fn = contexte.request.head if methode == "head" else contexte.request.get
                rep = await fn(url, timeout=8000, max_redirects=5)
                code = rep.status
                if methode == "head" and code in (403, 405, 501, 999):
                    continue  # certains serveurs refusent HEAD → on retente en GET
                return code
            except Exception:
                if methode == "get":
                    return "erreur"
        return "erreur"


# Codes qui NE signifient PAS un lien réellement cassé : un humain qui clique y
# accède (anti-bot, rate-limit, mur de connexion). On ne les compte pas comme
# « cassés » pour ne pas noyer l'utilisateur sous des faux positifs.
CODES_NON_CASSES = {401, 403, 429, 451, 503, 999}

# Plateformes qui bloquent agressivement les robots (renvoient 4xx à un crawler
# alors qu'un vrai navigateur connecté accède). Sur ces hôtes, un 4xx ≠ cassé.
HOTES_ANTIBOT = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "t.co", "tiktok.com", "pinterest.com", "threads.net", "medium.com",
    "quora.com", "crunchbase.com",
}


def _hote_antibot(url: str) -> bool:
    try:
        h = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(h == d or h.endswith("." + d) for d in HOTES_ANTIBOT)


def lien_est_casse(statut, url: str = "") -> bool:
    if statut == "erreur":
        return True
    if isinstance(statut, int):
        if statut in CODES_NON_CASSES:
            return False
        if 400 <= statut < 500 and _hote_antibot(url):
            return False  # blocage anti-bot d'une plateforme sociale, pas un lien mort
        return statut >= 400
    return False


def lien_est_restreint(statut, url: str = "") -> bool:
    if isinstance(statut, int):
        if statut in CODES_NON_CASSES:
            return True
        if 400 <= statut < 500 and _hote_antibot(url):
            return True
    return False


# ============================================================
#  DÉTECTION DES PROBLÈMES (seuils → liste de problèmes par page)
# ============================================================

def detecter_problemes(res: dict) -> list[dict]:
    p: list[dict] = []
    t = res.get("tests", {})

    def add(cat, sev, msg, **extra):
        p.append({"categorie": cat, "severite": sev, "message": msg, **extra})

    statut = res.get("statut")
    if isinstance(statut, int) and statut >= 400:
        add("fiabilite", CRITIQUE, f"Statut HTTP {statut}")
    elif not isinstance(statut, int):
        add("fiabilite", CRITIQUE, f"Page non chargée ({statut})")

    # --- Performance ---
    perf = t.get("performance") or {}
    if perf:
        ttfb = perf.get("ttfb_ms")
        if ttfb is not None:
            if ttfb > 1500:
                add("performance", MAJEUR, f"Serveur lent à répondre : TTFB {ttfb} ms (cible < 800)")
            elif ttfb > 800:
                add("performance", MINEUR, f"TTFB à surveiller : {ttfb} ms (cible < 800)")
        lcp = perf.get("lcp_ms")
        if lcp is not None:
            if lcp > 4000:
                add("performance", MAJEUR, f"LCP lent : {lcp} ms (cible < 2500)")
            elif lcp > 2500:
                add("performance", MINEUR, f"LCP moyen : {lcp} ms (cible < 2500)")
        cls = perf.get("cls")
        if cls is not None:
            if cls > 0.25:
                add("performance", MAJEUR, f"CLS élevé : {cls} (cible < 0.1)")
            elif cls > 0.1:
                add("performance", MINEUR, f"CLS à surveiller : {cls} (cible < 0.1)")
        load = perf.get("load_ms")
        if load and load > 5000:
            add("performance", MAJEUR, f"Chargement long : {load} ms")
        elif load and load > 3000:
            add("performance", MINEUR, f"Chargement à optimiser : {load} ms")
        poids = perf.get("poids_total_ko")
        if poids and poids > 5000:
            add("performance", MAJEUR, f"Page lourde : {poids} ko transférés")
        elif poids and poids > 2500:
            add("performance", MINEUR, f"Page un peu lourde : {poids} ko")
        if perf.get("nb_requetes", 0) > 120:
            add("performance", MINEUR, f"Beaucoup de requêtes : {perf['nb_requetes']}")

        # images : poids gâché, CLS, images cassées, lazy-loading
        img = perf.get("images") or {}
        if img.get("cassees", 0) > 0:
            add("performance", MAJEUR, f"{img['cassees']} image(s) cassée(s) (ne se chargent pas)")
        if img.get("surdimensionnees", 0) > 0:
            ex = [f"{e['src']} ({e['naturel']}→{e['affiche']})" for e in img.get("exemples", [])[:4]]
            add("performance", MAJEUR if img["surdimensionnees"] > 3 else MINEUR,
                f"{img['surdimensionnees']} image(s) sur-dimensionnée(s) (poids gâché, surtout en bas débit)",
                details=ex)
        if img.get("sans_dimensions", 0) > 2:
            add("performance", MINEUR,
                f"{img['sans_dimensions']} image(s) sans width/height (provoque des sauts de mise en page)")
        if img.get("sans_lazy", 0) > 3:
            add("performance", MINEUR,
                f"{img['sans_lazy']} image(s) hors écran sans loading=\"lazy\"")

        # compression & cache des ressources
        liv = perf.get("livraison") or {}
        if liv.get("non_compresse"):
            add("performance", MAJEUR,
                f"{len(liv['non_compresse'])} ressource(s) texte non compressée(s) (gzip/brotli manquant)",
                details=liv["non_compresse"][:6])
        if liv.get("sans_cache"):
            add("performance", MINEUR,
                f"{len(liv['sans_cache'])} actif(s) statique(s) sans cache durable (Cache-Control)",
                details=liv["sans_cache"][:6])

        # render-blocking dans le <head>
        head = perf.get("head") or {}
        if head.get("css_bloquant", 0) > 4:
            add("performance", MINEUR, f"{head['css_bloquant']} feuille(s) CSS bloquant le rendu")
        if head.get("js_bloquant", 0) > 0:
            add("performance", MINEUR,
                f"{head['js_bloquant']} script(s) synchrone(s) dans le <head> (bloque le rendu ; ajoute defer/async)")

    # --- SEO ---
    seo = t.get("seo") or {}
    if seo:
        titre = seo.get("titre")
        if not titre:
            add("seo", MAJEUR, "Balise <title> absente")
        elif len(titre) < 10:
            add("seo", MINEUR, f"Titre trop court ({len(titre)} car.)")
        elif len(titre) > 65:
            add("seo", MINEUR, f"Titre trop long ({len(titre)} car., risque de troncature)")
        desc = seo.get("meta_description")
        if not desc:
            add("seo", MAJEUR, "Méta description absente")
        elif len(desc) < 50:
            add("seo", MINEUR, f"Méta description courte ({len(desc)} car.)")
        elif len(desc) > 160:
            add("seo", MINEUR, f"Méta description longue ({len(desc)} car.)")
        if seo.get("h1_count", 0) == 0:
            add("seo", MAJEUR, "Aucun <h1>")
        elif seo.get("h1_count", 0) > 1:
            add("seo", MINEUR, f"{seo['h1_count']} balises <h1> (idéalement une seule)")
        if not seo.get("viewport"):
            add("seo", MAJEUR, "Balise meta viewport absente (mauvais rendu mobile)")
        if not seo.get("canonical"):
            add("seo", MINEUR, "Lien canonical absent")
        if not seo.get("lang"):
            add("seo", MINEUR, "Attribut lang absent sur <html>")
        if seo.get("saut_hierarchie"):
            add("seo", MINEUR, "Hiérarchie de titres incohérente (saut de niveau)")
        sa = seo.get("images_sans_alt", 0)
        if sa > 0:
            sev = MAJEUR if sa > 5 else MINEUR
            add("seo", sev, f"{sa} image(s) sans attribut alt")
        if seo.get("jsonld_count", 0) == 0:
            add("seo", INFO, "Aucune donnée structurée (JSON-LD)")

    # sondes "tête de page" (peuvent venir de perf ou de seo selon les familles actives)
    head = (perf.get("head") if perf else None) or seo.get("_head") or {}
    if head:
        if head.get("placeholders"):
            add("fiabilite", MAJEUR,
                "Contenu de remplissage encore présent (texte provisoire)",
                details=head["placeholders"][:4])
        if not head.get("manifest"):
            add("seo", INFO, "Pas de manifeste PWA (link rel=manifest)")
        # i18n : un hreflang doit toujours inclure une valeur x-default
        hl = head.get("hreflang") or []
        if hl and "x-default" not in [h.lower() for h in hl]:
            add("seo", MINEUR, "hreflang présent mais sans x-default (i18n incomplet)")

    # --- Accessibilité ---
    a11y = t.get("accessibilite") or {}
    if a11y:
        if a11y.get("html_lang") is False:
            add("accessibilite", MAJEUR, "Attribut lang manquant sur <html>")
        if a11y.get("champs_sans_label", 0) > 0:
            add("accessibilite", MAJEUR, f"{a11y['champs_sans_label']} champ(s) de formulaire sans label")
        if a11y.get("boutons_sans_nom", 0) > 0:
            add("accessibilite", MAJEUR, f"{a11y['boutons_sans_nom']} bouton(s) sans nom accessible")
        if a11y.get("liens_sans_nom", 0) > 0:
            add("accessibilite", MINEUR, f"{a11y['liens_sans_nom']} lien(s) sans texte accessible")
        imp_sev = {"critical": CRITIQUE, "serious": MAJEUR, "moderate": MINEUR, "minor": INFO}
        for v in (a11y.get("axe_violations") or []):
            add("accessibilite", imp_sev.get(v["impact"], MINEUR),
                f"axe[{v['impact']}] {v['id']} — {v['description']} ({v['noeuds']} él.)",
                details=v.get("cibles") or [])

    # zoom bloqué = barrière d'accessibilité majeure (malvoyants)
    if head.get("zoom_bloque"):
        add("accessibilite", MAJEUR, "Zoom désactivé (user-scalable=no / maximum-scale) — bloque les malvoyants")

    # --- Responsive ---
    # Message STABLE (sans les coupables) pour que l'agrégat regroupe par viewport ;
    # la liste des coupables va dans 'details'.
    resp = t.get("responsive") or {}
    for nom, d in resp.items():
        if d.get("scroll_horizontal"):
            coupables = [c["el"] for c in d.get("coupables", [])[:5]]
            add("responsive", MAJEUR,
                f"Débordement horizontal en {nom} ({d.get('largeur')}px)",
                details=coupables)
        # ergonomie tactile mesurée au viewport mobile
        tac = d.get("tactile") or {}
        if tac.get("cibles_petites", 0) > 2:
            ex = [f"{e['el']} {e['w']}×{e['h']}px" for e in tac.get("exemples_petits", [])[:4]]
            add("responsive", MINEUR,
                f"{tac['cibles_petites']} cible(s) tactile(s) trop petite(s) (<24px) en {nom}",
                details=ex)
        if tac.get("polices_minuscules", 0) > 8:
            add("responsive", MINEUR,
                f"{tac['polices_minuscules']} bloc(s) de texte en police < 12px (dur à lire sur mobile)")

    # --- Sécurité ---
    sec = t.get("securite") or {}
    if sec:
        if not sec.get("https"):
            add("securite", CRITIQUE, "Page servie en HTTP (pas de HTTPS)")
        if sec.get("strict_transport_security") == "absent":
            add("securite", MINEUR, "En-tête HSTS absent")
        if sec.get("content_security_policy") == "absent":
            add("securite", MINEUR, "Content-Security-Policy absente")
        elif sec.get("csp_unsafe"):
            add("securite", INFO, "CSP présente mais avec unsafe-inline/unsafe-eval")
        if sec.get("x_frame_options") == "absent":
            add("securite", MINEUR, "X-Frame-Options absent (risque de clickjacking)")
        if sec.get("x_content_type_options") == "absent":
            add("securite", MINEUR, "X-Content-Type-Options absent")
        if sec.get("referrer_policy") == "absent":
            add("securite", INFO, "Referrer-Policy absente")
        if sec.get("contenu_mixte", 0) > 0:
            add("securite", MAJEUR, f"{sec['contenu_mixte']} ressource(s) en HTTP (contenu mixte)")
        if sec.get("blank_sans_noopener", 0) > 0:
            add("securite", MINEUR, f"{sec['blank_sans_noopener']} lien(s) target=_blank sans rel=noopener")
        if sec.get("cookies_faibles"):
            noms = [c["nom"] for c in sec["cookies_faibles"][:6]]
            add("securite", MINEUR,
                f"{len(sec['cookies_faibles'])} cookie(s) sans attribut de sécurité",
                details=noms)

    # --- Fiabilité (console / JS / requêtes) ---
    nb_js = len(res.get("erreurs_js", []))
    if nb_js:
        add("fiabilite", MAJEUR, f"{nb_js} erreur(s) JavaScript non interceptée(s)")
    nb_console = len(res.get("erreurs_console", []))
    if nb_console:
        add("fiabilite", MINEUR, f"{nb_console} erreur(s) dans la console")
    nb_req = len(res.get("requetes_echouees", []))
    if nb_req:
        add("fiabilite", MINEUR, f"{nb_req} requête(s) en échec (4xx/5xx/réseau)")

    # --- Formulaires soumis ---
    for f in (t.get("formulaires") or []):
        if f.get("soumis") and f.get("erreurs_affichees"):
            add("fiabilite", MINEUR,
                f"Formulaire « {f.get('type_probable', 'form')} » : erreurs à la soumission",
                details=f["erreurs_affichees"])
        elif f.get("erreur_soumission"):
            add("fiabilite", MINEUR,
                f"Formulaire « {f.get('type_probable', 'form')} » : échec technique de soumission")

    p.sort(key=lambda x: ORDRE_SEVERITE[x["severite"]])
    return p


def score_page(problemes: list[dict], categorie: str | None = None) -> int:
    s = 100
    for pb in problemes:
        if categorie is None or pb["categorie"] == categorie:
            s -= POIDS[pb["severite"]]
    return max(0, s)


# ============================================================
#  RAPPORTS
# ============================================================

def construire_rapport(cfg: Config, auditeur: Auditeur) -> dict:
    pages = auditeur.resultats

    # liens cassés vs restreints (anti-bot / rate-limit) : on ne mélange pas, pour
    # ne pas faire passer un 429 GitHub ou un 403 anti-bot pour un lien mort.
    casses, restreints = [], []
    for cible, sources in auditeur.tous_liens.items():
        st = auditeur.statut_lien.get(cible)
        if lien_est_casse(st, cible):
            casses.append({"cible": cible, "statut": st, "sources": sorted(sources)[:8]})
        elif lien_est_restreint(st, cible):
            restreints.append({"cible": cible, "statut": st, "sources": sorted(sources)[:4]})
    casses.sort(key=lambda x: str(x["statut"]))
    restreints.sort(key=lambda x: str(x["statut"]))

    # Score par catégorie : moyenne, PAR PAGE, des pages où la catégorie a tourné.
    # (une page inaccessible compte comme un CRITIQUE dans 'fiabilite').
    scores = {}
    for cat in CATEGORIES:
        vals = []
        for pg in pages:
            if cat == "fiabilite" or cat in pg.get("tests", {}):
                vals.append(score_page(pg.get("problemes", []), cat))
        scores[cat] = round(sum(vals) / len(vals)) if vals else None
    # Score global : moyenne des catégories réellement mesurées (cohérent avec les
    # barres affichées). Avec --only, ne reflète donc que les familles demandées.
    dispo = [v for v in scores.values() if v is not None]
    scores["global"] = round(sum(dispo) / len(dispo)) if dispo else None

    # agrégat des problèmes
    agr = {}
    for pg in pages:
        for pb in pg.get("problemes", []):
            cle = (pb["categorie"], pb["severite"], re.sub(r"\d+", "N", pb["message"]))
            agr.setdefault(cle, {"categorie": pb["categorie"], "severite": pb["severite"],
                                 "exemple": pb["message"], "pages": []})
            agr[cle]["pages"].append(pg["url"])
    problemes_globaux = sorted(
        ({**v, "occurrences": len(v["pages"]), "pages": v["pages"][:10]} for v in agr.values()),
        key=lambda x: (ORDRE_SEVERITE[x["severite"]], -x["occurrences"]),
    )

    # problèmes au niveau du SITE (une fois, pas par page) : redirection HTTPS,
    # soft-404, sitemap. On les place en tête de la liste prioritaire.
    site = auditeur.infos_site or {}
    site_pb = []
    if site.get("http_redirige_https") is False:
        site_pb.append({"categorie": "securite", "severite": MAJEUR, "occurrences": 1, "pages": [],
                        "exemple": "Le site ne redirige pas http:// vers https:// (accessible en clair)"})
    if site.get("soft_404") is True:
        site_pb.append({"categorie": "fiabilite", "severite": MAJEUR, "occurrences": 1, "pages": [],
                        "exemple": f"Soft-404 : une URL inexistante renvoie 200 au lieu de 404 "
                                   f"(testé : code {site.get('code_404_teste')})"})
    if site.get("www_canonique") is False:
        site_pb.append({"categorie": "seo", "severite": MINEUR, "occurrences": 1, "pages": [],
                        "exemple": f"www / apex non canonicalisés : {site.get('www_alt')} ne redirige pas vers le domaine principal"})
    if site.get("sitemap") is False:
        site_pb.append({"categorie": "seo", "severite": MINEUR, "occurrences": 1, "pages": [],
                        "exemple": "Aucun sitemap.xml trouvé (ni via robots.txt ni à /sitemap.xml)"})
    problemes_globaux = site_pb + problemes_globaux

    return {
        "meta": {
            "url": cfg.url,
            "domaine": cfg.domaine,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "pages_auditees": len(pages),
            "familles": sorted(cfg.familles),
            "mobile": cfg.mobile,
            "lent": cfg.lent,
            "site": site,
        },
        "scores": scores,
        "problemes_globaux": problemes_globaux,
        "liens_casses": casses,
        "liens_restreints": restreints,
        "pages": pages,
    }


def afficher_resume(rapport: dict):
    m = rapport["meta"]
    sc = rapport["scores"]
    print("\n" + "=" * 60)
    print(f"  RAPPORT — {m['domaine']}  ({m['pages_auditees']} pages)")
    print("=" * 60)

    def barre(v):
        if v is None:
            return "n/a"
        plein = round(v / 10)
        return f"{'█' * plein}{'░' * (10 - plein)} {v}/100"

    site = m.get("site") or {}
    if site.get("tech"):
        ligne = ", ".join(site["tech"])
        if site.get("spa"):
            ligne += "  (application monopage / SPA)"
        print(f"\n  🧱 Pile détectée : {ligne}")
    if site.get("trackers"):
        print(f"  📊 Trackers : {', '.join(site['trackers'])}")
    if m.get("lent"):
        print("  🐢 Mesuré en mode bas débit (~3G)")

    print(f"\n  Score global : {barre(sc.get('global'))}")
    for cat in CATEGORIES:
        print(f"    {cat:<14} {barre(sc.get(cat))}")

    nb = {CRITIQUE: 0, MAJEUR: 0, MINEUR: 0, INFO: 0}
    for pb in rapport["problemes_globaux"]:
        nb[pb["severite"]] += pb["occurrences"]
    print(f"\n  Problèmes : {nb[CRITIQUE]} critiques · {nb[MAJEUR]} majeurs · "
          f"{nb[MINEUR]} mineurs · {nb[INFO]} infos")

    icone = {CRITIQUE: "🛑", MAJEUR: "⚠️ ", MINEUR: "🔹", INFO: "ℹ️ "}
    print("\n  ── Principaux problèmes ──")
    for pb in rapport["problemes_globaux"][:18]:
        occ = f"×{pb['occurrences']}" if pb["occurrences"] > 1 else ""
        print(f"  {icone[pb['severite']]} [{pb['categorie']}] {pb['exemple']} {occ}")

    if rapport["liens_casses"]:
        print(f"\n  🔗 {len(rapport['liens_casses'])} lien(s) cassé(s) :")
        for l in rapport["liens_casses"][:10]:
            print(f"     {l['statut']}  {l['cible'][:80]}")
    if rapport.get("liens_restreints"):
        print(f"\n  🔒 {len(rapport['liens_restreints'])} lien(s) restreint(s) (anti-bot/connexion, PAS cassés) :")
        for l in rapport["liens_restreints"][:5]:
            print(f"     {l['statut']}  {l['cible'][:80]}")

    print("\n" + "=" * 60)


# ---- rapport HTML (gabarit statique + données JSON injectées) ----

GABARIT_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audit — __DOMAINE__</title>
<style>
  :root { --bg:#0f1115; --carte:#1a1d24; --txt:#e6e8eb; --doux:#9aa3ad; --bord:#2a2f3a;
          --crit:#ff5d5d; --maj:#ffa23e; --min:#5ab0ff; --info:#7f8896; --ok:#3ddc84; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--txt); }
  .wrap { max-width:1080px; margin:0 auto; padding:32px 20px 80px; }
  h1 { font-size:24px; margin:0 0 4px; } .doux { color:var(--doux); }
  .cartes { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin:24px 0; }
  .carte { background:var(--carte); border:1px solid var(--bord); border-radius:12px; padding:16px; }
  .carte .v { font-size:30px; font-weight:700; } .carte .l { color:var(--doux); font-size:13px; text-transform:capitalize; }
  .anneau { width:64px; height:64px; border-radius:50%; display:grid; place-items:center; font-weight:700; margin-bottom:8px; }
  .sec { background:var(--carte); border:1px solid var(--bord); border-radius:12px; margin:16px 0; overflow:hidden; }
  .sec > summary { cursor:pointer; padding:14px 18px; font-weight:600; list-style:none; display:flex; justify-content:space-between; align-items:center; }
  .sec > summary::-webkit-details-marker { display:none; }
  .corps { padding:0 18px 16px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  td,th { text-align:left; padding:7px 8px; border-bottom:1px solid var(--bord); vertical-align:top; }
  th { color:var(--doux); font-weight:500; }
  .pill { display:inline-block; padding:1px 9px; border-radius:20px; font-size:12px; font-weight:600; }
  .b-critique{background:rgba(255,93,93,.15);color:var(--crit)} .b-majeur{background:rgba(255,162,62,.15);color:var(--maj)}
  .b-mineur{background:rgba(90,176,255,.15);color:var(--min)} .b-info{background:rgba(127,136,150,.18);color:var(--info)}
  a { color:var(--min); text-decoration:none; } a:hover { text-decoration:underline; }
  .miniature { max-width:160px; border-radius:8px; border:1px solid var(--bord); }
  code { background:#0c0e12; padding:1px 5px; border-radius:5px; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Audit web — <span id="domaine"></span></h1>
  <div class="doux" id="meta"></div>
  <div class="cartes" id="scores"></div>
  <div id="contenu"></div>
</div>
<script>
const D = __DONNEES__;
const SEV = ['critique','majeur','mineur','info'];
const coul = v => v==null?'#7f8896':(v>=90?'#3ddc84':v>=70?'#ffa23e':v>=50?'#ff8a3e':'#ff5d5d');
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pill = sev => `<span class="pill b-${sev}">${sev}</span>`;

document.getElementById('domaine').textContent = D.meta.domaine;
const S = D.meta.site || {};
let metaLigne = `${D.meta.pages_auditees} pages · ${D.meta.date} · familles : ${D.meta.familles.join(', ')}`;
if (D.meta.lent) metaLigne += ' · 🐢 bas débit';
document.getElementById('meta').innerHTML = esc(metaLigne)
  + ((S.tech && S.tech.length) ? `<br>🧱 ${esc(S.tech.join(', '))}${S.spa?' (SPA)':''}` : '')
  + ((S.trackers && S.trackers.length) ? ` · 📊 ${esc(S.trackers.join(', '))}` : '');

// cartes de score
const cats = ['global'].concat(Object.keys(D.scores).filter(c=>c!=='global'));
document.getElementById('scores').innerHTML = cats.map(c=>{
  const v = D.scores[c];
  return `<div class="carte"><div class="anneau" style="background:conic-gradient(${coul(v)} ${(v||0)*3.6}deg,#2a2f3a 0)">
    <div style="width:48px;height:48px;border-radius:50%;background:var(--carte);display:grid;place-items:center">${v==null?'–':v}</div></div>
    <div class="l">${c}</div></div>`;
}).join('');

let html = '';

// problèmes globaux
const nb = {critique:0,majeur:0,mineur:0,info:0};
D.problemes_globaux.forEach(p=>nb[p.severite]+=p.occurrences);
html += `<details class="sec" open><summary><span>🔎 Problèmes prioritaires</span>
  <span>${nb.critique} crit · ${nb.majeur} maj · ${nb.mineur} min</span></summary><div class="corps">
  <table><thead><tr><th>Sév.</th><th>Catégorie</th><th>Problème</th><th>Pages</th></tr></thead><tbody>` +
  D.problemes_globaux.map(p=>`<tr><td>${pill(p.severite)}</td><td>${esc(p.categorie)}</td>
    <td>${esc(p.exemple)}</td><td>${p.occurrences}</td></tr>`).join('') +
  `</tbody></table></div></details>`;

// liens cassés
if (D.liens_casses.length) {
  html += `<details class="sec"><summary><span>🔗 Liens cassés</span><span>${D.liens_casses.length}</span></summary><div class="corps">
    <table><thead><tr><th>Statut</th><th>URL</th><th>Trouvé sur</th></tr></thead><tbody>` +
    D.liens_casses.map(l=>`<tr><td>${esc(l.statut)}</td><td>${esc(l.cible)}</td>
      <td>${l.sources.map(s=>esc(s)).join('<br>')}</td></tr>`).join('') +
    `</tbody></table></div></details>`;
}

// liens restreints (anti-bot / connexion requise) — informatif, pas des bugs
if ((D.liens_restreints||[]).length) {
  html += `<details class="sec"><summary><span>🔒 Liens restreints (anti-bot / connexion — non cassés)</span><span>${D.liens_restreints.length}</span></summary><div class="corps">
    <table><thead><tr><th>Statut</th><th>URL</th></tr></thead><tbody>` +
    D.liens_restreints.map(l=>`<tr><td>${esc(l.statut)}</td><td>${esc(l.cible)}</td></tr>`).join('') +
    `</tbody></table></div></details>`;
}

// détail par page
html += D.pages.map(pg=>{
  const probs = (pg.problemes||[]);
  const det = p => (Array.isArray(p.details) && p.details.length)
    ? `<div class="doux" style="font-size:12px"><code>${p.details.map(esc).join('</code> <code>')}</code></div>` : '';
  const lignes = probs.map(p=>`<tr><td>${pill(p.severite)}</td><td>${esc(p.categorie)}</td><td>${esc(p.message)}${det(p)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="doux">Aucun problème détecté 🎉</td></tr>';
  const cap = pg.capture ? `<img class="miniature" src="${esc(pg.capture)}" loading="lazy">` : '';
  // formulaires testés
  const forms = ((pg.tests||{}).formulaires)||[];
  const formHtml = forms.length ? `<div style="margin-top:12px"><b>Formulaires (${forms.length})</b>` +
    forms.map(f=>{
      const etat = f.soumis ? (f.succes_probable ? '✅ soumis, succès probable'
        : (f.erreurs_affichees&&f.erreurs_affichees.length ? '⚠️ soumis avec erreurs' : '➡️ soumis'))
        : `⏸️ ${esc(f.soumission||'non soumis')}`;
      const ch = Object.entries(f.champs_remplis||{}).map(([k,v])=>`<code>${esc(k)}=${esc(v)}</code>`).join(' ');
      const err = (f.erreurs_affichees||[]).map(esc).join(' · ');
      return `<div style="margin:6px 0;padding:8px;border:1px solid var(--bord);border-radius:8px">
        <div>${esc(f.type_probable||'form')} — ${etat}</div>
        <div class="doux" style="font-size:12px;margin-top:4px">${ch}</div>
        ${err?`<div style="color:var(--maj);font-size:12px;margin-top:4px">${err}</div>`:''}
        ${f.url_apres&&f.redirige?`<div class="doux" style="font-size:12px">→ ${esc(f.url_apres)}</div>`:''}
      </div>`;
    }).join('') + `</div>` : '';
  return `<details class="sec"><summary><span>${esc(pg.titre||pg.url)}</span>
    <span class="doux">${esc(pg.statut)} · ${probs.length} pb</span></summary><div class="corps">
    <div class="doux" style="margin-bottom:8px"><a href="${esc(pg.url)}" target="_blank">${esc(pg.url)}</a></div>
    <table>${lignes}</table>${formHtml}
    <div style="margin-top:12px">${cap}</div></div></details>`;
}).join('');

document.getElementById('contenu').innerHTML = html;
</script>
</body>
</html>
"""


def generer_html(rapport: dict, chemin: Path):
    donnees = json.dumps(rapport, ensure_ascii=False)
    # neutralise </script> et autres séquences qui casseraient le bloc <script>
    donnees = donnees.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = GABARIT_HTML.replace("__DONNEES__", donnees).replace("__DOMAINE__", rapport["meta"]["domaine"])
    chemin.write_text(html, encoding="utf-8")


# ============================================================
#  MODE SCÉNARIO (parcours multi-étapes : inscription, tunnel, etc.)
# ============================================================

async def _eval_safe(page, js, defaut=None):
    try:
        return await page.evaluate(js)
    except Exception:
        return defaut


def _resume_etape(e: dict) -> str:
    a = e.get("action", "?")
    for k in ("texte", "placeholder", "label", "selecteur", "nom", "url",
              "bouton_actif", "bouton_inactif", "texte_present", "url_contient", "visible", "valeur"):
        if e.get(k):
            return f"{a} : {e[k]}"
    return a


def _valeur_etape(etape: dict, profil: dict) -> str:
    if "valeur" in etape:
        return str(etape["valeur"])
    if "profil" in etape:
        return str((profil or {}).get(etape["profil"], ""))
    return ""


async def _cliquable(page, etape):
    """Renvoie (locator, description) pour un clic ; priorise bouton/lien par leur texte."""
    if etape.get("selecteur"):
        return page.locator(etape["selecteur"]).first, etape["selecteur"]
    t = etape.get("texte")
    if t:
        for getter in (lambda: page.get_by_role("button", name=t, exact=False),
                       lambda: page.get_by_role("link", name=t, exact=False),
                       lambda: page.get_by_text(t, exact=False)):
            try:
                loc = getter().first
                if await loc.count() > 0:
                    return loc, t
            except Exception:
                pass
        return None, t
    return None, "(cible non spécifiée)"


async def _localiser(page, etape):
    """Renvoie un locator pour un champ (remplir/cocher/choisir)."""
    if etape.get("selecteur"):
        return page.locator(etape["selecteur"]).first
    if etape.get("placeholder"):
        return page.get_by_placeholder(etape["placeholder"]).first
    if etape.get("label"):
        return page.get_by_label(etape["label"]).first
    if etape.get("nom"):
        return page.locator(f'[name="{etape["nom"]}"]').first
    return None


async def _verifier(page, etape):
    if "bouton_actif" in etape:
        nom = etape["bouton_actif"]
        loc = page.get_by_role("button", name=nom, exact=False).first
        if await loc.count() == 0:
            return {"ok": False, "raison": f"bouton « {nom} » introuvable"}
        dis = await loc.is_disabled()
        return {"ok": not dis, "raison": None if not dis else f"le bouton « {nom} » est resté DÉSACTIVÉ"}
    if "bouton_inactif" in etape:
        nom = etape["bouton_inactif"]
        loc = page.get_by_role("button", name=nom, exact=False).first
        dis = (await loc.count() == 0) or await loc.is_disabled()
        return {"ok": dis, "raison": None if dis else f"le bouton « {nom} » est actif alors qu'il devrait être désactivé"}
    if "texte_present" in etape:
        t = etape["texte_present"]
        ok = await page.get_by_text(t, exact=False).first.count() > 0
        return {"ok": ok, "raison": None if ok else f"texte attendu absent : « {t} »"}
    if "url_contient" in etape:
        ok = etape["url_contient"] in page.url
        return {"ok": ok, "raison": None if ok else f"l'URL ne contient pas « {etape['url_contient']} » (actuelle : {page.url})"}
    if "visible" in etape:
        v = etape["visible"]
        loc = page.locator(v).first if v[:1] in ".#[/" else page.get_by_text(v, exact=False).first
        ok = await loc.count() > 0 and await loc.is_visible()
        return {"ok": ok, "raison": None if ok else f"élément non visible : {v}"}
    return {"ok": False, "raison": "vérification sans critère (bouton_actif / texte_present / url_contient / visible)"}


async def _executer_etape(page, etape: dict, profil: dict):
    action = (etape.get("action") or "").lower()
    to = 8000
    if action in ("aller", "goto"):
        await page.goto(etape["url"], wait_until="domcontentloaded", timeout=20000)
        return {"ok": True}
    if action in ("attendre", "wait"):
        if etape.get("texte"):
            await page.get_by_text(etape["texte"], exact=False).first.wait_for(timeout=etape.get("ms", 8000))
        elif etape.get("selecteur"):
            await page.locator(etape["selecteur"]).first.wait_for(timeout=etape.get("ms", 8000))
        else:
            await page.wait_for_timeout(etape.get("ms", 1000))
        return {"ok": True}
    if action in ("cliquer", "click"):
        loc, desc = await _cliquable(page, etape)
        if loc is None:
            return {"ok": False, "raison": f"élément introuvable : « {desc} »"}
        try:
            if await loc.is_disabled():
                return {"ok": False, "raison": f"« {desc} » est désactivé — clic impossible"}
        except Exception:
            pass
        try:
            await loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        await loc.click(timeout=to)
        return {"ok": True}
    if action in ("remplir", "fill"):
        loc = await _localiser(page, etape)
        if loc is None or await loc.count() == 0:
            return {"ok": False, "raison": "champ à remplir introuvable (placeholder/label/selecteur/nom)"}
        val = _valeur_etape(etape, profil)
        await loc.fill(val, timeout=to)
        if etape.get("presser"):
            await page.keyboard.press(etape["presser"])
        cible = (str(etape.get("selecteur", "")) + str(etape.get("placeholder", "")) + str(etape.get("profil", ""))).lower()
        masque = etape.get("profil") == "mot_de_passe" or "password" in cible or "passe" in cible
        return {"ok": True, "valeur": "••••••" if masque else val}
    if action in ("choisir", "select"):
        loc = await _localiser(page, etape)
        if loc is None:
            return {"ok": False, "raison": "liste déroulante introuvable"}
        await loc.select_option(etape.get("valeur"))
        return {"ok": True}
    if action in ("cocher", "check"):
        loc = await _localiser(page, etape)
        if loc is None:
            return {"ok": False, "raison": "case à cocher introuvable"}
        await loc.check(timeout=to)
        return {"ok": True}
    if action in ("presser", "press"):
        await page.keyboard.press(etape.get("touche", "Enter"))
        return {"ok": True}
    if action in ("verifier", "assert"):
        return await _verifier(page, etape)
    if action in ("capture", "screenshot"):
        return {"ok": True}
    return {"ok": False, "raison": f"action inconnue : « {action} »"}


async def jouer_scenario(cfg, sc: dict = None, dossier: Path = None) -> dict:
    sc = sc or cfg.scenario
    dossier = dossier or cfg.dossier
    profil = cfg.profil or {}
    etapes = sc.get("etapes", [])
    (dossier / "etapes").mkdir(parents=True, exist_ok=True)
    url0 = sc.get("url") or cfg.url
    resultats, bloque_a = [], None

    async with async_playwright() as p:
        try:
            nav = await p.chromium.launch(headless=not cfg.headful)
        except Exception as e:
            sys.exit("❌ Chromium introuvable. Lance : playwright install chromium\n   (" + str(e).splitlines()[0] + ")")
        vp = VIEWPORTS["mobile"] if cfg.mobile else VIEWPORTS["bureau"]
        ctx = await nav.new_context(viewport=vp, ignore_https_errors=True,
                                    is_mobile=cfg.mobile, storage_state=cfg.storage_state)
        ctx.set_default_timeout(cfg.timeout)
        page = await ctx.new_page()
        errs_console = []
        page.on("console", lambda m: errs_console.append(m.text[:200]) if m.type == "error" else None)

        await page.goto(url0, wait_until="domcontentloaded", timeout=cfg.timeout)
        for etat, dl in (("load", cfg.timeout), ("networkidle", 6000)):
            try:
                await page.wait_for_load_state(etat, timeout=dl)
            except Exception:
                pass
        await page.wait_for_timeout(800)

        print(f"🎬 Scénario « {sc.get('nom', 'sans nom')} » — {len(etapes)} étapes sur {url0}")
        for i, etape in enumerate(etapes, 1):
            desc = etape.get("decrire") or _resume_etape(etape)
            avant = len(errs_console)
            print(f"  [{i:>2}/{len(etapes)}] {desc}")
            try:
                r = await _executer_etape(page, etape, profil)
            except Exception as e:
                r = {"ok": False, "raison": f"erreur technique : {str(e).splitlines()[0][:160]}"}
            await page.wait_for_timeout(etape.get("apres_ms", 900))
            cap = dossier / "etapes" / f"{i:02d}.png"
            try:
                await page.screenshot(path=str(cap), full_page=True)
            except Exception:
                pass
            msgs = await _eval_safe(page, JS_ERREURS_FORM, defaut=[])
            etat = {
                "n": i, "action": etape.get("action"), "description": desc,
                "ok": bool(r.get("ok")), "raison": r.get("raison"),
                "valeur": r.get("valeur"), "url": page.url,
                "erreurs_page": (msgs or [])[:5],
                "erreurs_console": errs_console[avant:][:5],
                "capture": cap.relative_to(dossier).as_posix() if cap.exists() else None,
            }
            resultats.append(etat)
            print(f"        {'✅' if etat['ok'] else '🛑'} {etat['raison'] or 'ok'}")
            if not etat["ok"]:
                bloque_a = etat
                break

        await ctx.close()
        await nav.close()

    return {
        "type": "scenario",
        "nom": sc.get("nom", "scénario"),
        "url": url0,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_etapes": len(etapes),
        "etapes_jouees": len(resultats),
        "reussi": bloque_a is None,
        "bloque_a": bloque_a,
        "etapes": resultats,
    }


# ============================================================
#  MODE PARCOURS AUTOMATIQUE (explore les tunnels SANS scénario)
# ============================================================

# Liste les champs remplissables, VISIBLES, activés et encore vides ; les marque (data-auto-idx).
JS_AUTO_CHAMPS = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none' && !el.disabled; };
  const lab = el => { if(el.labels&&el.labels[0])return el.labels[0].textContent||'';
    const a=el.getAttribute('aria-label'); if(a)return a;
    const l=el.getAttribute('aria-labelledby'); if(l){const n=document.getElementById(l); if(n)return n.textContent||'';}
    return ''; };
  const out=[]; let i=0;
  const sel='input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]),textarea,select';
  for(const el of document.querySelectorAll(sel)){
    if(!vis(el))continue;
    const tag=el.tagName.toLowerCase(), type=(el.type||'text').toLowerCase();
    if(tag!=='select'&&type!=='checkbox'&&type!=='radio'&&el.value)continue;
    if((type==='checkbox'||type==='radio')&&el.checked)continue;
    el.setAttribute('data-auto-idx',i);
    out.push({idx:i,tag,type,name:el.name||'',id:el.id||'',
      placeholder:el.getAttribute('placeholder')||'',label:(lab(el)||'').trim().slice(0,80),
      autocomplete:el.getAttribute('autocomplete')||'',
      required:!!(el.required||el.getAttribute('aria-required')==='true'),
      options:tag==='select'?Array.from(el.options).map(o=>({v:o.value,t:(o.textContent||'').trim()})):null});
    i++;
  }
  return out;
}"""

# Repère le bouton « avancer » le plus probable (visible, même désactivé) ; le marque (data-auto-btn).
JS_AUTO_BOUTON = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
  const prio=['continuer','suivante','suivant','poursuivr','next','valider','confirmer','commencer',
    'démarr','demarr','terminer','enregistr','soumettre','envoyer','accéd','acced','créer','creer',
    'inscri','se connecter','connecter','connexion','login','sign in','entrer','payer','finalis',
    'parti','essay','gratuit','get started','démarrer','start'];
  // jamais un bouton « retour / changer / annuler » : il ferait reculer ou boucler le parcours
  const recul=['retour','précédent','precedent','revenir','changer','modifier','annuler','back','←','‹'];
  // on inclut les liens (a[href]) car beaucoup de CTA « Créer / Commencer » sont des <a> (Next.js <Link>)
  const cands=Array.from(document.querySelectorAll('button,[role=button],input[type=submit],a[href]'));
  let best=null,bestS=-2;
  for(const el of cands){
    if(!vis(el))continue;
    const t=((el.innerText||el.value||el.getAttribute('aria-label')||'')+'').trim().toLowerCase();
    if(!t)continue;
    if(recul.some(b=>t.includes(b)))continue;
    let s=-2;
    for(let k=0;k<prio.length;k++){ if(t.includes(prio[k])){ s=prio.length-k; break; } }
    if(s<-1 && (el.type==='submit')) s=-1;
    if(s>bestS){bestS=s;best=el;}
  }
  if(!best||bestS<-1)return {trouve:false};
  document.querySelectorAll('[data-auto-btn]').forEach(e=>e.removeAttribute('data-auto-btn'));
  best.setAttribute('data-auto-btn','1');
  const t=((best.innerText||best.value||'')+'').trim().slice(0,40);
  const dis=best.disabled||best.getAttribute('aria-disabled')==='true';
  const final=/créer|creer|inscri|payer|finalis/.test(t.toLowerCase());
  return {trouve:true,texte:t,disabled:!!dis,final:final};
}"""

# Signature d'un écran (pour détecter une progression / une boucle).
JS_AUTO_SIGNATURE = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(); return r.width>0&&r.height>0; };
  const heads=Array.from(document.querySelectorAll('h1,h2,h3,legend,[role=heading]'))
    .filter(vis).map(e=>(e.textContent||'').trim()).filter(Boolean).slice(0,3).join(' | ');
  const fields=Array.from(document.querySelectorAll('input:not([type=hidden]),select,textarea'))
    .filter(vis).map(e=>((e.name||e.id||e.type||'')+'').toLowerCase()).sort().join(',');
  return location.pathname+'##'+heads+'##'+fields;
}"""

JS_AUTO_TITRE = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(); return r.width>0&&r.height>0; };
  const h=Array.from(document.querySelectorAll('h1,h2,h3,legend,[role=heading],label'))
    .filter(vis).map(e=>(e.textContent||'').trim()).filter(Boolean);
  return (h[0]||'').slice(0,90);
}"""

# Sur un écran de CHOIX (pas de champ ni de bouton « continuer »), repère une option à cliquer.
JS_AUTO_CHOIX = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none' && !el.disabled; };
  const bad=['retour','annuler','précédent','precedent','revenir','changer','modifier','back','←','‹',
    'déconnexion','deconnexion','accueil','aide','support','mentions','politique','conditions','cookie',
    'fermer','close','langue','français','francais','anglais','english','passer','skip',
    'déjà un compte','deja un compte'];
  document.querySelectorAll('[data-auto-btn]').forEach(e=>e.removeAttribute('data-auto-btn'));
  for(const el of document.querySelectorAll('button,[role=button],a[role=button]')){
    if(!vis(el))continue;
    const t=((el.innerText||el.getAttribute('aria-label')||'')+'').trim();
    const tl=t.toLowerCase();
    if(!t||t.length>70)continue;
    if(bad.some(b=>tl.includes(b)))continue;
    el.setAttribute('data-auto-btn','1');
    return {trouve:true,texte:t.slice(0,40)};
  }
  return {trouve:false};
}"""

JS_AUTO_SUCCES = r"""() => {
  const t=((document.body&&document.body.innerText)||'').toLowerCase();
  const u=location.href.toLowerCase();
  const mots=['compte créé','compte cree','bienvenue','félicitation','felicitation','inscription réussie',
    'inscription reussie','votre compte a','confirme ton email','confirmez votre','vérifie ton email',
    'vérifiez votre','email de confirmation','merci pour votre inscription','tableau de bord'];
  const urls=['/dashboard','/welcome','/bienvenue','/merci','/success','/accueil','/home','/app'];
  return mots.some(m=>t.includes(m))||urls.some(x=>u.includes(x));
}"""


def _est_soumission_finale(texte: str, champs: list) -> bool:
    """Le clic va-t-il VRAIMENT créer un compte / payer ? (≠ simple navigation vers le tunnel).
    Un paiement est toujours « final » ; une création/inscription ne l'est que si on soumet un mot de passe."""
    t = (texte or "").lower()
    if any(m in t for m in ("payer", "finalis", "régler", "regler", "commander", "souscrire", "acheter")):
        return True
    if any(m in t for m in ("créer", "creer", "inscri", "terminer")):
        return any((c.get("type") or "").lower() == "password" for c in (champs or []))
    return False


def _soumission_ok(cfg) -> bool:
    """La soumission réelle (création de compte / paiement) est-elle autorisée ?"""
    if not cfg.soumettre_formulaires:
        return False
    h = hote(cfg.url)
    test = (h in ("localhost", "127.0.0.1", "0.0.0.0") or h.endswith(".local")
            or any(m in h for m in ("staging", "preview", "sandbox", "dev.", "-dev", ".test")))
    return test or cfg.autoriser_prod


def _valeur_auto(c: dict, profil: dict, etat: dict):
    """Choisit quoi faire d'un champ : ('fill', val) / ('select', val) / ('check', True) / None."""
    tag = c.get("tag")
    typ = (c.get("type") or "text").lower()
    if tag == "select":
        for o in (c.get("options") or []):
            v = (o.get("v") or "").strip()
            t = (o.get("t") or "").strip().lower()
            if v and t not in ("", "choisir", "choisir…", "sélectionner", "selectionner", "—", "-"):
                return ("select", o["v"])
        return None
    if typ == "checkbox":
        foin = " ".join([c.get("name", ""), c.get("id", ""), c.get("label", "")]).lower()
        if c.get("required") or any(m in foin for m in (
                "accept", "conditions", "cgu", "cgv", "terms", "politique", "règlement",
                "reglement", "consent", "j'accepte", "jaccepte", "obligatoire")):
            return ("check", True)
        return None  # newsletter & options facultatives : laissées décochées
    if typ == "radio":
        return ("check", True)  # on coche le premier choix du groupe
    infos = {"type": typ, "tag": tag, "autocomplete": c.get("autocomplete", ""),
             "name": c.get("name", ""), "id": c.get("id", ""),
             "placeholder": c.get("placeholder", ""), "label": c.get("label", "")}
    val = _valeur_champ(infos, profil, etat)
    return ("fill", val) if val is not None else None


async def explorer_parcours_auto(cfg, dossier: Path, url0: str = None) -> dict:
    """Explore un tunnel SANS scénario : remplit les champs et clique « Continuer » jusqu'au blocage.
    Renvoie un rapport au même format que jouer_scenario (réutilise le HTML scénario)."""
    profil = cfg.profil or {}
    etat = {}
    url0 = url0 or cfg.url
    (dossier / "etapes").mkdir(parents=True, exist_ok=True)
    resultats, bloque_a, vues = [], None, set()
    maxi = cfg.auto_max_etapes

    async with async_playwright() as p:
        try:
            nav = await p.chromium.launch(headless=not cfg.headful)
        except Exception as e:
            sys.exit("❌ Chromium introuvable. Lance : playwright install chromium\n   (" + str(e).splitlines()[0] + ")")
        vp = VIEWPORTS["mobile"] if cfg.mobile else VIEWPORTS["bureau"]
        ctx = await nav.new_context(viewport=vp, ignore_https_errors=True,
                                    is_mobile=cfg.mobile, storage_state=cfg.storage_state)
        ctx.set_default_timeout(cfg.timeout)
        page = await ctx.new_page()
        errs_console = []
        page.on("console", lambda m: errs_console.append(m.text[:200]) if m.type == "error" else None)
        await page.goto(url0, wait_until="domcontentloaded", timeout=cfg.timeout)
        for st, dl in (("load", cfg.timeout), ("networkidle", 6000)):
            try:
                await page.wait_for_load_state(st, timeout=dl)
            except Exception:
                pass
        await page.wait_for_timeout(700)

        print(f"🧭 Exploration auto depuis {url0}  (max {maxi} écrans)")
        for i in range(1, maxi + 1):
            titre = await _eval_safe(page, JS_AUTO_TITRE, "") or f"écran {i}"
            sig = await _eval_safe(page, JS_AUTO_SIGNATURE, str(i))
            avant = len(errs_console)

            # 1) remplir tous les champs visibles encore vides
            champs = await _eval_safe(page, JS_AUTO_CHAMPS, []) or []
            remplis, radios_vus = [], set()
            for c in champs:
                if (c.get("type") or "").lower() == "radio":
                    grp = c.get("name") or c.get("id")
                    if grp in radios_vus:
                        continue
                    radios_vus.add(grp)
                choix = _valeur_auto(c, profil, etat)
                if not choix:
                    continue
                mode, val = choix
                loc = page.locator(f'[data-auto-idx="{c["idx"]}"]').first
                try:
                    if mode == "select":
                        await loc.select_option(val, timeout=4000)
                    elif mode == "check":
                        await loc.check(timeout=4000)
                    else:
                        await loc.fill(str(val), timeout=4000)
                    lbl = (c.get("label") or c.get("placeholder") or c.get("name") or c.get("type") or "champ")[:48]
                    masque = (c.get("type") == "password"
                              or "passe" in (c.get("name", "") + c.get("label", "")).lower())
                    remplis.append({"champ": lbl, "valeur": "••••••" if masque else str(val)})
                except Exception:
                    pass

            # blur pour déclencher la validation (certains formulaires n'activent qu'après)
            await _eval_safe(page, "() => { if(document.activeElement) document.activeElement.blur(); }")
            await page.wait_for_timeout(500)

            # 2) bouton pour avancer
            btn = await _eval_safe(page, JS_AUTO_BOUTON, {"trouve": False}) or {"trouve": False}
            cap = dossier / "etapes" / f"{i:02d}.png"
            try:
                await page.screenshot(path=str(cap), full_page=True)
            except Exception:
                pass
            et = {"n": i, "action": "auto", "description": f"Écran {i} : {titre}",
                  "url": page.url,
                  "valeur": ", ".join(f"{r['champ']}={r['valeur']}" for r in remplis) or None,
                  "erreurs_page": (await _eval_safe(page, JS_ERREURS_FORM, []) or [])[:5],
                  "erreurs_console": errs_console[avant:][:5],
                  "capture": cap.relative_to(dossier).as_posix() if cap.exists() else None}

            # bouton « Continuer » présent mais DÉSACTIVÉ après remplissage = blocage (le cas ibi)
            if btn.get("trouve") and btn.get("disabled"):
                et["ok"] = False
                et["raison"] = (f"le bouton « {btn.get('texte')} » reste DÉSACTIVÉ "
                                f"après avoir rempli les champs de cet écran")
                resultats.append(et); bloque_a = et
                print(f"  [{i:>2}] {titre} → 🛑 {et['raison']}")
                break

            # garde-fou : ne pas créer de compte / payer réellement sur prod
            if btn.get("trouve") and _est_soumission_finale(btn.get("texte"), champs) and not _soumission_ok(cfg):
                et["ok"] = True
                et["raison"] = (f"parcours OK jusqu'à « {btn.get('texte')} » — soumission finale NON envoyée "
                                f"(sécurité prod ; ajoute --soumettre-formulaires --autoriser-prod pour finaliser)")
                resultats.append(et)
                print(f"  [{i:>2}] {titre} → ✅ stop avant soumission finale « {btn.get('texte')} »")
                break

            # Quoi cliquer pour avancer ? bouton « Continuer », sinon (écran de choix) une option.
            cible = btn.get("texte") if btn.get("trouve") else None
            via_choix = False
            if not btn.get("trouve") and not champs:
                choix = await _eval_safe(page, JS_AUTO_CHOIX, {"trouve": False}) or {"trouve": False}
                if choix.get("trouve"):
                    cible, via_choix = choix.get("texte"), True

            if cible is None:
                succ = await _eval_safe(page, JS_AUTO_SUCCES, False)
                et["ok"] = True
                et["raison"] = ("parcours terminé (page finale atteinte)" if succ
                                else ("fin du parcours (plus rien à cliquer)" if i > 1
                                      else "aucun formulaire/parcours détecté sur cette page"))
                resultats.append(et)
                print(f"  [{i:>2}] {titre} → {'✅' if succ or i > 1 else 'ℹ️'} {et['raison']}")
                break

            et["ok"] = True
            verbe = "choix" if via_choix else "clic"
            et["raison"] = (f"écran rempli ({len(remplis)} champ(s)) puis {verbe} « {cible} »"
                            if remplis else f"{verbe} « {cible} »")
            resultats.append(et)
            print(f"  [{i:>2}] {titre} → {verbe} « {cible} »")
            url_avant = page.url
            n_pages = len(ctx.pages)
            try:
                await page.locator('[data-auto-btn="1"]').first.click(timeout=8000)
            except Exception as e:
                et["ok"] = False
                et["raison"] = f"clic impossible sur « {cible} » : {str(e).splitlines()[0][:120]}"
                bloque_a = et
                break
            await page.wait_for_timeout(900)
            # un nouvel onglet s'est ouvert (target=_blank) ? on bascule dessus
            if len(ctx.pages) > n_pages:
                page = ctx.pages[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=6000)
                except Exception:
                    pass
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

            nsig = await _eval_safe(page, JS_AUTO_SIGNATURE, str(i) + "x")
            avance = (nsig != sig) or (page.url != url_avant)
            if not avance:
                errs = await _eval_safe(page, JS_ERREURS_FORM, []) or []
                capb = dossier / "etapes" / f"{i:02d}b.png"
                try:
                    await page.screenshot(path=str(capb), full_page=True)
                except Exception:
                    pass
                # mur de connexion : un écran de login avec de faux identifiants n'est PAS un bug
                a_mdp = any((c.get("type") or "").lower() == "password" for c in champs)
                login = a_mdp or any(m in (cible or "").lower()
                                     for m in ("connect", "connex", "login", "sign in"))
                if errs:
                    ok, raison = False, "message d'erreur affiché : " + " | ".join(errs)
                elif login:
                    ok, raison = True, ("connexion non aboutie — identifiants de test refusés ou compte requis "
                                        "(donne un --profil avec de vrais identifiants pour explorer la suite)")
                else:
                    ok, raison = False, "rien ne se passe après le clic (parcours bloqué)"
                bl = {"n": i + 1, "action": "auto",
                      "description": f"Après « {cible} » : la page n'avance pas",
                      "ok": ok, "raison": raison, "url": page.url, "valeur": None,
                      "erreurs_page": errs[:5], "erreurs_console": [],
                      "capture": capb.relative_to(dossier).as_posix() if capb.exists() else None}
                resultats.append(bl)
                if not ok:
                    bloque_a = bl
                print(f"  [{i:>2}] → {'🛑' if not ok else 'ℹ️'} {raison}")
                break
            if nsig in vues:
                print("  ↩️  boucle détectée — arrêt.")
                break
            vues.add(sig)
            if await _eval_safe(page, JS_AUTO_SUCCES, False):
                resultats.append({"n": i + 1, "action": "auto", "description": "Page finale atteinte",
                                  "ok": True, "raison": "parcours terminé avec succès", "url": page.url,
                                  "valeur": None, "erreurs_page": [], "erreurs_console": [], "capture": None})
                print("  ✅ page finale atteinte")
                break

        await ctx.close()
        await nav.close()

    return {
        "type": "scenario",
        "nom": "Parcours auto (exploration sans scénario)",
        "url": url0,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_etapes": len(resultats),
        "etapes_jouees": len(resultats),
        "reussi": bloque_a is None,
        "bloque_a": bloque_a,
        "etapes": resultats,
    }


def afficher_resume_scenario(rapport: dict):
    print("\n" + "=" * 60)
    print(f"  SCÉNARIO — {rapport['nom']}")
    print("=" * 60)
    for e in rapport["etapes"]:
        print(f"  {'✅' if e['ok'] else '🛑'} [{e['n']:>2}] {e['description']}"
              + (f"  → {e['raison']}" if e["raison"] else ""))
    if rapport["reussi"]:
        print(f"\n  ✅ Scénario complété : {rapport['etapes_jouees']}/{rapport['total_etapes']} étapes.")
    else:
        b = rapport["bloque_a"]
        print(f"\n  🛑 BLOQUÉ à l'étape {b['n']}/{rapport['total_etapes']} : {b['description']}")
        print(f"     Raison : {b['raison']}")
        if b.get("erreurs_page"):
            print(f"     Message(s) à l'écran : {' | '.join(b['erreurs_page'])}")
        if b.get("erreurs_console"):
            print(f"     Erreur(s) console : {' | '.join(b['erreurs_console'])}")
        print(f"     Capture : {b.get('capture')}")
    print("=" * 60)


GABARIT_SCENARIO_HTML = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Scénario — __NOM__</title>
<style>
 :root{--bg:#0f1115;--carte:#1a1d24;--txt:#e6e8eb;--doux:#9aa3ad;--bord:#2a2f3a;--ok:#3ddc84;--ko:#ff5d5d}
 *{box-sizing:border-box} body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
 .wrap{max-width:920px;margin:0 auto;padding:32px 20px 80px} h1{font-size:22px;margin:0 0 4px} .doux{color:var(--doux)}
 .banniere{padding:14px 18px;border-radius:12px;margin:18px 0;font-weight:600}
 .b-ok{background:rgba(61,220,132,.12);color:var(--ok);border:1px solid var(--ok)}
 .b-ko{background:rgba(255,93,93,.12);color:var(--ko);border:1px solid var(--ko)}
 .etape{display:flex;gap:14px;background:var(--carte);border:1px solid var(--bord);border-radius:12px;padding:14px;margin:10px 0}
 .etape.ko{border-color:var(--ko)} .num{font-weight:700;width:30px;flex:none;color:var(--doux)}
 .pastille{font-size:18px;flex:none} .miniature{max-width:200px;border-radius:8px;border:1px solid var(--bord);margin-top:8px}
 .err{color:var(--ko);font-size:13px;margin-top:4px} code{background:#0c0e12;padding:1px 5px;border-radius:5px;font-size:12px}
</style></head><body><div class="wrap">
<h1>Scénario — <span id="nom"></span></h1><div class="doux" id="meta"></div>
<div id="banniere"></div><div id="etapes"></div></div>
<script>
const D=__DONNEES__;
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('nom').textContent=D.nom;
document.getElementById('meta').textContent=`${D.url} · ${D.date} · ${D.etapes_jouees}/${D.total_etapes} étapes`;
const b=D.bloque_a;
document.getElementById('banniere').innerHTML = D.reussi
 ? `<div class="banniere b-ok">✅ Scénario complété (${D.etapes_jouees}/${D.total_etapes})</div>`
 : `<div class="banniere b-ko">🛑 Bloqué à l'étape ${b.n}/${D.total_etapes} — ${esc(b.description)}<br><span style="font-weight:400">${esc(b.raison||'')}</span></div>`;
document.getElementById('etapes').innerHTML = D.etapes.map(e=>`
 <div class="etape ${e.ok?'':'ko'}">
   <div class="num">${e.n}</div><div class="pastille">${e.ok?'✅':'🛑'}</div>
   <div style="flex:1">
     <div><b>${esc(e.description)}</b>${e.valeur?` <code>${esc(e.valeur)}</code>`:''}</div>
     <div class="doux" style="font-size:12px">${esc(e.action)} · ${esc(e.url)}</div>
     ${e.raison?`<div class="err">${esc(e.raison)}</div>`:''}
     ${(e.erreurs_page||[]).length?`<div class="err">écran : ${e.erreurs_page.map(esc).join(' · ')}</div>`:''}
     ${e.capture?`<img class="miniature" src="${esc(e.capture)}" loading="lazy">`:''}
   </div></div>`).join('');
</script></body></html>
"""


def generer_html_scenario(rapport: dict, chemin: Path):
    donnees = json.dumps(rapport, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = GABARIT_SCENARIO_HTML.replace("__DONNEES__", donnees).replace("__NOM__", rapport["nom"])
    chemin.write_text(html, encoding="utf-8")


# ============================================================
#  CLI
# ============================================================

def parser_args() -> Config:
    ap = argparse.ArgumentParser(description="Auditeur web — teste n'importe quel site.")
    ap.add_argument("url", nargs="?", help="URL de départ, ex: https://monsite.com "
                                           "(optionnel si --scenario fournit déjà l'URL)")
    ap.add_argument("--scenario", action="append", metavar="FICHIER.json",
                    help="parcours multi-étapes à rejouer (inscription, tunnel…) ; voir scenario.exemple.json. "
                         "Répétable : --scenario a.json --scenario b.json")
    ap.add_argument("--avec-audit", action="store_true",
                    help="quand des --scenario / --auto sont fournis, lancer AUSSI l'audit générique "
                         "(les deux analyses dans un même dossier, avec un index.html combiné)")
    ap.add_argument("--auto", action="store_true",
                    help="EXPLORE automatiquement les parcours : remplit les formulaires et clique "
                         "« Continuer/Suivant » d'écran en écran jusqu'au blocage — sans scénario JSON")
    ap.add_argument("--auto-max-etapes", type=int, default=14,
                    help="nombre max d'écrans explorés en mode --auto (défaut 14)")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--concurrence", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=20000, help="ms par navigation")
    ap.add_argument("--mobile", action="store_true", help="émule un mobile (viewport + UA)")
    ap.add_argument("--lent", action="store_true",
                    help="bride la connexion (~3G, 400 kbps, CPU ×4) pour tester l'expérience bas débit")
    ap.add_argument("--garder-cookies", action="store_true",
                    help="ne PAS fermer les bannières de consentement (par défaut : on les ferme)")
    ap.add_argument("--sous-domaines", action="store_true", help="crawler aussi les sous-domaines")
    ap.add_argument("--soumettre-formulaires", action="store_true", help="⚠️ soumet réellement les formulaires")
    ap.add_argument("--autoriser-prod", action="store_true",
                    help="⚠️ autorise la soumission réelle même hors site de test (localhost/staging)")
    ap.add_argument("--profil", help="fichier JSON avec tes infos pour remplir les formulaires "
                                     "(voir profil.exemple.json)")
    ap.add_argument("--ignorer-robots", action="store_true", help="ne pas respecter robots.txt")
    ap.add_argument("--headful", action="store_true", help="afficher le navigateur")
    ap.add_argument("--only", help="familles à exécuter, séparées par des virgules "
                                   "(performance,seo,accessibilite,responsive,securite,liens,formulaires)")
    ap.add_argument("--inclure", help="regex : ne crawler que les URLs qui matchent")
    ap.add_argument("--exclure", help="regex : ignorer les URLs qui matchent")
    ap.add_argument("--auth", help="fichier storage_state.json (session connectée Playwright)")
    ap.add_argument("--sortie", help="dossier de sortie (défaut: rapport_<domaine>_<date>)")
    ap.add_argument("--client", action="store_true",
                    help="génère AUSSI rapport-client.html : diagnostic commercial sans jargon, "
                         "autonome (capture incluse), à envoyer tel quel à un commerçant")
    ap.add_argument("--prestataire", metavar="FICHIER.json",
                    help="coordonnées affichées dans le rapport client (nom, email, telephone, site, titre). "
                         "Défaut : prestataire.json à côté du script, s'il existe")
    ap.add_argument("--client-depuis", metavar="RAPPORT.json",
                    help="(re)génère uniquement le rapport client depuis un rapport.json existant, sans re-crawler")
    a = ap.parse_args()

    # coordonnées du prestataire pour le rapport client (explicite, sinon prestataire.json local)
    prestataire = None
    if a.client or a.client_depuis:
        chemin_p = Path(a.prestataire) if a.prestataire else Path(__file__).resolve().parent / "prestataire.json"
        if a.prestataire and not chemin_p.is_file():
            ap.error(f"--prestataire : fichier introuvable : {a.prestataire}")
        if chemin_p.is_file():
            try:
                prestataire = json.loads(chemin_p.read_text(encoding="utf-8"))
                if not isinstance(prestataire, dict):
                    raise ValueError("doit être un objet JSON")
            except Exception as e:
                ap.error(f"prestataire : JSON invalide ({chemin_p}) : {e}")

    # --- Mode régénération : rapport client seul, depuis un rapport.json déjà produit ---
    if a.client_depuis:
        src = Path(a.client_depuis)
        if not src.is_file():
            ap.error(f"--client-depuis : fichier introuvable : {a.client_depuis}")
        try:
            rapport = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:
            ap.error(f"--client-depuis : JSON invalide : {e}")
        cible = src.parent / "rapport-client.html"
        generer_rapport_client(rapport, cible, prestataire, dossier=src.parent)
        print(f"🤝 Rapport client : {cible}")
        sys.exit(0)

    # mode scénario : charge le ou les fichiers de parcours
    scenarios = []
    for chemin in (a.scenario or []):
        chemin_sc = Path(chemin)
        if not chemin_sc.is_file():
            ap.error(f"--scenario : fichier introuvable : {chemin}")
        try:
            sc = json.loads(chemin_sc.read_text(encoding="utf-8"))
        except Exception as e:
            ap.error(f"--scenario : JSON invalide ({chemin}) : {e}")
        if not isinstance(sc, dict) or not isinstance(sc.get("etapes"), list):
            ap.error(f"--scenario : « {chemin} » doit être un objet JSON avec une liste « etapes »")
        sc["_fichier"] = chemin_sc.stem
        scenarios.append(sc)
    scenario = scenarios[0] if scenarios else None
    # faire l'audit générique si : ni scénario ni --auto (mode normal), ou --avec-audit explicite
    faire_audit = (not scenarios and not a.auto) or a.avec_audit

    # URL : positionnelle, ou tirée du scénario
    brute = a.url or (scenario or {}).get("url")
    if not brute:
        ap.error("URL manquante : donne une URL, ou un --scenario contenant une clé « url »")
    url = brute if "://" in brute else "https://" + brute
    domaine = hote(url)
    if not domaine:
        ap.error(f"URL invalide : {brute}")

    # familles : valide les noms passés à --only ('fiabilite' est implicite)
    familles = set(CATEGORIES + ["liens", "formulaires"])
    valides = set(CATEGORIES + ["liens", "formulaires"]) - {"fiabilite"}
    if a.only:
        demande = {x.strip() for x in a.only.split(",") if x.strip()}
        inconnues = demande - valides
        if inconnues:
            ap.error(f"--only : famille(s) inconnue(s) : {', '.join(sorted(inconnues))}. "
                     f"Valides : {', '.join(sorted(valides))}")
        familles = demande | {"fiabilite"}

    # compile/valide les regex tout de suite (sinon crash plus tard dans le crawl)
    inclure = exclure = None
    try:
        if a.inclure:
            inclure = re.compile(a.inclure)
        if a.exclure:
            exclure = re.compile(a.exclure)
    except re.error as e:
        ap.error(f"regex --inclure/--exclure invalide : {e}")

    # valide le fichier --auth avant de lancer le navigateur
    if a.auth:
        chemin_auth = Path(a.auth)
        if not chemin_auth.is_file():
            ap.error(f"--auth : fichier introuvable : {a.auth}")
        try:
            json.loads(chemin_auth.read_text(encoding="utf-8"))
        except Exception as e:
            ap.error(f"--auth : storage_state JSON invalide : {e}")

    # charge le profil (infos perso) — via --profil, sinon via la clé "profil" du scénario
    profil = None
    chemin_profil = a.profil or (scenario or {}).get("profil")
    if chemin_profil:
        cp = Path(chemin_profil)
        if not cp.is_file():
            ap.error(f"--profil : fichier introuvable : {chemin_profil}")
        try:
            profil = json.loads(cp.read_text(encoding="utf-8"))
            if not isinstance(profil, dict):
                raise ValueError("le profil doit être un objet JSON")
        except Exception as e:
            ap.error(f"--profil : JSON invalide : {e}")

    if a.sortie:
        dossier = Path(a.sortie)
    else:
        horo = datetime.now().strftime("%Y%m%d_%H%M")
        prefixe = "parcours" if ((scenarios or a.auto) and not faire_audit) else "rapport"
        dossier = Path(f"{prefixe}_{domaine}_{horo}")

    return Config(
        url=url, domaine=domaine, sous_domaines=a.sous_domaines,
        max_pages=a.max_pages, max_depth=a.max_depth, concurrence=a.concurrence,
        timeout=a.timeout, respecter_robots=not a.ignorer_robots,
        soumettre_formulaires=a.soumettre_formulaires, autoriser_prod=a.autoriser_prod,
        profil=profil, scenario=scenario, scenarios=scenarios, faire_audit=faire_audit,
        auto=a.auto, auto_max_etapes=a.auto_max_etapes,
        headful=a.headful, mobile=a.mobile,
        lent=a.lent, gerer_cookies=not a.garder_cookies,
        inclure=inclure, exclure=exclure, storage_state=a.auth,
        dossier=dossier, familles=familles,
        client=a.client, prestataire=prestataire,
    )


def generer_html_index(cfg, rapport_audit: dict, parcours: list, chemin: Path):
    """Page d'accueil combinée : audit générique + parcours (scénarios), avec liens et verdicts."""
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    blocs = []

    # --- Carte audit générique ---
    if rapport_audit:
        sc = rapport_audit.get("scores", {}) or {}
        nb = {"critique": 0, "majeur": 0, "mineur": 0, "info": 0}
        for pb in rapport_audit.get("problemes_globaux", []):
            nb[pb["severite"]] = nb.get(pb["severite"], 0) + pb.get("occurrences", 1)
        g = sc.get("global")
        couleur = "ok" if (g or 0) >= 80 else ("maj" if (g or 0) >= 50 else "ko")
        lignes_scores = " · ".join(
            f"{lib} <b>{sc.get(cle, '–')}</b>" for cle, lib in (
                ("performance", "Perf"), ("seo", "SEO"), ("accessibilite", "A11y"),
                ("responsive", "Resp"), ("securite", "Sécu"), ("fiabilite", "Fiab")))
        blocs.append(f"""
        <a class="carte lien" href="rapport.html">
          <div class="titre">📊 Audit complet
            <span class="score {couleur}">{g if g is not None else '–'}</span></div>
          <div class="doux">{esc(rapport_audit.get('meta', {}).get('pages_auditees', '?'))} page(s) ·
            <span class="ko">{nb['critique']} critiques</span> ·
            <span class="maj">{nb['majeur']} majeurs</span></div>
          <div class="doux pet">{lignes_scores}</div>
          <div class="cta">Ouvrir le rapport détaillé →</div>
        </a>""")

    # --- Cartes parcours (scénarios) ---
    for sc, rap, lien in parcours:
        if rap.get("reussi"):
            verdict = f'<span class="badge b-ok">✅ Parcours complété ({rap["etapes_jouees"]}/{rap["total_etapes"]})</span>'
            detail = "Toutes les étapes ont réussi."
        else:
            b = rap.get("bloque_a") or {}
            verdict = f'<span class="badge b-ko">🛑 Bloqué à l\'étape {b.get("n", "?")}/{rap["total_etapes"]}</span>'
            detail = f'{esc(b.get("description", ""))} — <span class="ko">{esc(b.get("raison", ""))}</span>'
        blocs.append(f"""
        <a class="carte lien" href="{esc(lien)}">
          <div class="titre">🎬 {esc(rap.get('nom', 'Parcours'))} {verdict}</div>
          <div class="doux pet">{detail}</div>
          <div class="cta">Voir le détail des étapes →</div>
        </a>""")

    domaine = cfg.domaine or (rapport_audit or {}).get("meta", {}).get("domaine", "")
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analyse — {esc(domaine)}</title>
<style>
 :root{{--bg:#0f1115;--carte:#1a1d24;--txt:#e6e8eb;--doux:#9aa3ad;--bord:#2a2f3a;
       --acc:#5ab0ff;--ok:#3ddc84;--ko:#ff5d5d;--maj:#ffa23e;}}
 *{{box-sizing:border-box}} body{{margin:0;font:16px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}}
 .wrap{{max-width:820px;margin:0 auto;padding:40px 22px 80px}}
 h1{{font-size:24px;margin:0 0 4px}} .doux{{color:var(--doux)}} .pet{{font-size:13px;margin-top:6px}}
 .ko{{color:var(--ko)}} .maj{{color:var(--maj)}} .ok{{color:var(--ok)}}
 .carte{{display:block;background:var(--carte);border:1px solid var(--bord);border-radius:14px;
        padding:20px 22px;margin-top:18px;text-decoration:none;color:var(--txt)}}
 .lien:hover{{border-color:var(--acc)}}
 .titre{{font-size:18px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
 .cta{{margin-top:12px;color:var(--acc);font-weight:600;font-size:14px}}
 .score{{font-size:15px;padding:2px 10px;border-radius:20px;font-weight:800}}
 .score.ok{{background:rgba(61,220,132,.15);color:var(--ok)}}
 .score.maj{{background:rgba(255,162,62,.15);color:var(--maj)}}
 .score.ko{{background:rgba(255,93,93,.15);color:var(--ko)}}
 .badge{{font-size:13px;padding:3px 10px;border-radius:20px;font-weight:700}}
 .b-ok{{background:rgba(61,220,132,.12);color:var(--ok);border:1px solid var(--ok)}}
 .b-ko{{background:rgba(255,93,93,.12);color:var(--ko);border:1px solid var(--ko)}}
</style></head>
<body><div class="wrap">
 <h1>🔍 Analyse de {esc(domaine)}</h1>
 <div class="doux">{date} · audit générique + parcours interactifs</div>
 {''.join(blocs)}
</div></body></html>"""
    chemin.write_text(html, encoding="utf-8")


async def executer_audit(cfg) -> dict:
    """Lance l'audit générique (crawl) et écrit rapport.json/html. Renvoie le rapport."""
    (cfg.dossier / "captures").mkdir(parents=True, exist_ok=True)
    print(f"🚀 Audit de {cfg.url}  →  dossier : {cfg.dossier}/")
    print(f"   max_pages={cfg.max_pages} depth={cfg.max_depth} concurrence={cfg.concurrence} "
          f"familles={sorted(cfg.familles)}")
    if cfg.profil:
        print(f"   👤 Profil chargé : {cfg.profil.get('email') or cfg.profil.get('nom_complet') or 'oui'}")
    if cfg.soumettre_formulaires:
        h = hote(cfg.url)
        test = (h in ("localhost", "127.0.0.1", "0.0.0.0") or h.endswith(".local")
                or any(m in h for m in ("staging", "preview", "sandbox", "dev.", "-dev", ".test")))
        if test or cfg.autoriser_prod:
            print(f"   ⚠️  SOUMISSION RÉELLE ACTIVE sur {h} — de vrais comptes/messages peuvent être créés.")
        else:
            print(f"   🛡️  Soumission demandée mais BLOQUÉE sur {h} (prod). "
                  f"Ajoute --autoriser-prod pour soumettre réellement.")

    auditeur = Auditeur(cfg)
    await auditeur.lancer()

    rapport = construire_rapport(cfg, auditeur)
    (cfg.dossier / "rapport.json").write_text(
        json.dumps(rapport, ensure_ascii=False, indent=2), encoding="utf-8")
    generer_html(rapport, cfg.dossier / "rapport.html")
    if cfg.client:
        generer_rapport_client(rapport, cfg.dossier / "rapport-client.html",
                               cfg.prestataire, dossier=cfg.dossier)
    afficher_resume(rapport)
    print(f"\n📄 JSON : {cfg.dossier / 'rapport.json'}")
    print(f"🌐 HTML : {cfg.dossier / 'rapport.html'}")
    if cfg.client:
        print(f"🤝 Rapport client : {cfg.dossier / 'rapport-client.html'}")
    print(f"📸 Captures : {cfg.dossier / 'captures'}/")
    return rapport


async def jouer_un_scenario(cfg, sc: dict, dossier: Path) -> dict:
    """Rejoue un parcours dans son propre sous-dossier ; écrit scenario.json/html."""
    dossier.mkdir(parents=True, exist_ok=True)
    rapport = await jouer_scenario(cfg, sc=sc, dossier=dossier)
    (dossier / "scenario.json").write_text(
        json.dumps(rapport, ensure_ascii=False, indent=2), encoding="utf-8")
    generer_html_scenario(rapport, dossier / "scenario.html")
    afficher_resume_scenario(rapport)
    return rapport


async def main():
    cfg = parser_args()
    cfg.dossier.mkdir(parents=True, exist_ok=True)

    # --- Cas historique : un seul scénario, sans audit ni auto → tout à la racine ---
    if cfg.scenarios and not cfg.faire_audit and not cfg.auto and len(cfg.scenarios) == 1:
        print(f"🎬 Scénario  →  dossier : {cfg.dossier}/")
        if cfg.profil:
            print(f"   👤 Profil : {cfg.profil.get('email') or cfg.profil.get('nom_complet') or 'oui'}")
        rapport = await jouer_scenario(cfg)
        (cfg.dossier / "scenario.json").write_text(
            json.dumps(rapport, ensure_ascii=False, indent=2), encoding="utf-8")
        generer_html_scenario(rapport, cfg.dossier / "scenario.html")
        afficher_resume_scenario(rapport)
        print(f"\n📄 JSON : {cfg.dossier / 'scenario.json'}")
        print(f"🌐 HTML : {cfg.dossier / 'scenario.html'}")
        print(f"📸 Captures : {cfg.dossier / 'etapes'}/")
        return

    # --- Mode combiné (et/ou multi-scénarios) ---
    rapport_audit = None
    if cfg.faire_audit:
        rapport_audit = await executer_audit(cfg)

    parcours = []  # [(sc, rapport, lien_html_relatif)]
    if cfg.scenarios:
        base = cfg.dossier / "parcours"
        slugs_vus = {}
        for sc in cfg.scenarios:
            slug = sc.get("_fichier") or re.sub(r"[^A-Za-z0-9._-]+", "-", sc.get("nom", "parcours"))[:40] or "parcours"
            slugs_vus[slug] = slugs_vus.get(slug, 0) + 1
            if slugs_vus[slug] > 1:
                slug = f"{slug}-{slugs_vus[slug]}"
            dsc = base / slug
            print(f"\n🎬 Parcours « {sc.get('nom', slug)} »  →  {dsc}/")
            rap = await jouer_un_scenario(cfg, sc, dsc)
            parcours.append((sc, rap, f"parcours/{slug}/scenario.html"))

    # exploration AUTOMATIQUE des tunnels (sans scénario)
    if cfg.auto:
        dauto = cfg.dossier / "parcours" / "auto"
        print(f"\n🧭 Parcours automatique  →  {dauto}/")
        rap = await explorer_parcours_auto(cfg, dauto, cfg.url)
        (dauto / "scenario.json").write_text(
            json.dumps(rap, ensure_ascii=False, indent=2), encoding="utf-8")
        generer_html_scenario(rap, dauto / "scenario.html")
        afficher_resume_scenario(rap)
        parcours.append((None, rap, "parcours/auto/scenario.html"))

    # index combiné dès qu'il y a au moins 2 artefacts (audit + parcours, ou plusieurs parcours)
    nb_artefacts = (1 if rapport_audit else 0) + len(parcours)
    if nb_artefacts >= 2:
        generer_html_index(cfg, rapport_audit, parcours, cfg.dossier / "index.html")
        print(f"\n🧩 Index combiné : {cfg.dossier / 'index.html'}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️  Interrompu.")
