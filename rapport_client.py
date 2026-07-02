# -*- coding: utf-8 -*-
"""
Rapport CLIENT — traduit l'audit technique en diagnostic commercial.

Pensé pour être envoyé tel quel à un commerçant / artisan / profession libérale
qui n'y connaît rien : pas de jargon, chaque constat est exprimé en conséquence
concrète pour son activité (visiteurs perdus, position Google, confiance).
Le fichier généré (rapport-client.html) est AUTONOME (capture d'écran incluse
en base64) : il s'envoie par email et s'imprime proprement en PDF (Ctrl+P).

Utilisé par auditeur.py via --client, ou seul via --client-depuis rapport.json.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

# ============================================================
#  TRADUCTION : constat technique → langage client
# ============================================================
# Chaque règle : (regex sur le message technique, clé de regroupement, titre client, impact client)
# Première règle qui matche gagne. Les messages qui partagent une clé sont fusionnés
# en un seul point client (ex : les 6 en-têtes de sécurité → 1 ligne).
# Un problème sans règle part dans « Autres points techniques » (bruts, en petit).

REGLES: list[tuple[str, str, str, str]] = [
    # --- Vitesse ---
    (r"Serveur lent à répondre|TTFB",
     "vitesse_serveur", "Votre serveur met du temps à répondre",
     "Chaque visiteur regarde un écran blanc avant de voir votre site. Une partie abandonne avant même l'affichage."),
    (r"LCP",
     "vitesse_affichage", "Le contenu principal s'affiche trop lentement",
     "Google mesure ce délai et fait reculer les sites lents dans les résultats ; les visiteurs impatients partent chez un concurrent."),
    (r"Chargement long|Chargement à optimiser",
     "vitesse_chargement", "Des pages sont longues à charger",
     "Au-delà de 3 secondes d'attente, une grande partie des visiteurs quitte la page — surtout sur téléphone."),
    (r"CLS|sans width/height",
     "vitesse_sauts", "La page « saute » pendant le chargement",
     "Les visiteurs cliquent au mauvais endroit et l'ensemble paraît peu soigné."),
    (r"image\(s\) cassée",
     "img_cassees", "Des images ne s'affichent pas",
     "Vos clients voient des cases vides à la place de vos photos : mauvaise impression immédiate."),
    (r"sur-dimensionnée|loading=.lazy.",
     "img_lourdes", "Vos images sont plus lourdes que nécessaire",
     "Les mêmes photos pourraient s'afficher aussi bien en plusieurs fois plus léger : le site serait nettement plus rapide, surtout sur mobile."),
    (r"Page lourde|un peu lourde",
     "page_lourde", "Des pages sont trop lourdes",
     "Elles consomment le forfait data de vos visiteurs et rallongent l'attente sur téléphone."),
    (r"non compressée",
     "compression", "Le site est envoyé sans compression",
     "Un simple réglage du serveur rendrait tout le site sensiblement plus rapide, sans rien changer au contenu."),
    (r"sans cache durable",
     "cache", "Le navigateur retélécharge tout à chaque visite",
     "Vos visiteurs réguliers attendent à chaque fois comme si c'était leur première visite."),
    (r"bloquant le rendu|script\(s\) synchrone|Beaucoup de requêtes",
     "rendu", "Des éléments techniques retardent l'affichage",
     "La page pourrait apparaître plus vite avec le même contenu, en réorganisant son chargement."),

    # --- Visibilité Google ---
    (r"<title> absente|Titre trop",
     "seo_titre", "Le titre de vos pages est absent ou mal calibré",
     "C'est la ligne bleue cliquable sur Google : elle décide si l'internaute clique sur vous ou sur un concurrent."),
    (r"Méta description",
     "seo_desc", "Pas de texte de présentation pour Google",
     "Sous votre nom, Google affiche un extrait pris au hasard au lieu d'un message qui donne envie de cliquer."),
    (r"Aucun <h1>|balises <h1>|Hiérarchie de titres",
     "seo_structure", "Le contenu des pages est mal structuré pour Google",
     "Google comprend moins bien de quoi parlent vos pages, donc les classe moins bien."),
    (r"meta viewport",
     "seo_viewport", "Le site ne se déclare pas compatible mobile",
     "Google pénalise les sites non adaptés au téléphone — c'est pourtant la majorité de vos visiteurs."),
    (r"sans attribut alt",
     "seo_alt", "Vos images sont invisibles pour Google",
     "Google Images ne peut pas référencer vos photos, et les personnes malvoyantes n'y ont pas accès."),
    (r"donnée structurée|JSON-LD",
     "seo_jsonld", "Google ne connaît pas votre fiche d'activité",
     "Vos horaires, votre adresse et vos avis pourraient s'afficher directement dans les résultats de recherche."),
    (r"sitemap",
     "seo_sitemap", "Pas de plan du site pour Google",
     "Google découvre vos pages moins vite et peut en oublier certaines."),
    (r"www / apex|canonical|lang absent sur|Attribut lang|hreflang",
     "seo_technique", "Réglages de référencement incomplets",
     "Des réglages simples qui aident Google à bien indexer le site sont absents."),

    # --- Mobile ---
    (r"Débordement horizontal",
     "mobile_deborde", "Le site déborde de l'écran sur téléphone",
     "Il faut faire glisser la page de gauche à droite pour lire : la plupart des visiteurs mobiles abandonnent."),
    (r"cible\(s\) tactile",
     "mobile_cibles", "Des boutons sont trop petits pour le doigt",
     "Sur téléphone, vos visiteurs peinent à cliquer au bon endroit (menu, téléphone, itinéraire…)."),
    (r"police < 12px",
     "mobile_polices", "Des textes sont trop petits sur mobile",
     "Vos clients doivent zoomer pour vous lire ; beaucoup ne le font pas."),

    # --- Sécurité ---
    (r"HTTP \(pas de HTTPS\)|ne redirige pas http",
     "secu_https", "La connexion à votre site n'est pas sécurisée",
     "Les navigateurs affichent « Non sécurisé » à côté de votre adresse : perte de confiance immédiate, et Google pénalise."),
    (r"contenu mixte",
     "secu_mixte", "Des éléments du site passent par une connexion non chiffrée",
     "Le cadenas de sécurité peut disparaître ou afficher un avertissement selon le navigateur."),
    (r"HSTS|Content-Security-Policy|CSP|X-Frame|X-Content-Type|Referrer-Policy|noopener|cookie\(s\) sans attribut",
     "secu_reglages", "Les réglages de sécurité du serveur sont incomplets",
     "Sans conséquence visible pour vos clients, mais un site mal configuré est plus facile à attaquer (piratage, faux contenu)."),

    # --- Accessibilité ---
    (r"Zoom désactivé",
     "a11y_zoom", "Le zoom est bloqué sur mobile",
     "Les personnes malvoyantes ou âgées ne peuvent pas agrandir le texte pour vous lire."),
    (r"champ\(s\) de formulaire sans label|bouton\(s\) sans nom|lien\(s\) sans texte|lang manquant sur",
     "a11y_manuel", "Des éléments sont inutilisables pour certains visiteurs",
     "Boutons ou champs sans nom : les personnes qui naviguent avec une aide (lecteur d'écran) sont bloquées."),
    (r"^axe\[",
     "a11y_axe", "Points d'accessibilité relevés (normes handicap)",
     "Le site gêne les personnes malvoyantes ou âgées. L'accessibilité est une obligation légale pour certaines activités, et Google en tient compte."),

    # --- Fiabilité ---
    (r"Statut HTTP|Page non chargée",
     "fiab_pages", "Des pages du site sont en erreur",
     "Vos visiteurs (et Google) tombent sur des pages qui ne se chargent pas."),
    (r"erreur\(s\) JavaScript",
     "fiab_js", "Des fonctions du site sont en erreur",
     "Certaines actions (menu, formulaire, boutons) peuvent ne pas fonctionner selon l'appareil du visiteur."),
    (r"Soft-404",
     "fiab_soft404", "Les pages supprimées ne sont pas signalées correctement",
     "Google continue d'indexer des pages qui n'existent plus : des visiteurs arrivent sur du vide."),
    (r"Contenu de remplissage",
     "fiab_placeholder", "Du texte provisoire est resté en ligne",
     "Des mentions type « lorem ipsum » ou « coming soon » sont visibles par vos clients : très mauvaise impression."),
    (r"Formulaire",
     "fiab_form", "Le formulaire du site présente des problèmes",
     "Des demandes de clients peuvent se perdre sans que vous le sachiez."),
    (r"erreur\(s\) dans la console|requête\(s\) en échec",
     "fiab_technique", "Des erreurs techniques se produisent en arrière-plan",
     "Signes de vieillissement du site ; à corriger avant que quelque chose de visible ne casse."),
]

_REGLES_COMPILEES = [(re.compile(rx), cle, titre, impact) for rx, cle, titre, impact in REGLES]

# Messages sans intérêt pour un client (jargon pur, déjà couvert ailleurs) : on les écarte.
_IGNORER = re.compile(r"manifeste PWA|x-default")

CATEGORIES_CLIENT = {
    "performance":   "Vitesse",
    "seo":           "Visibilité Google",
    "accessibilite": "Accessibilité",
    "responsive":    "Affichage mobile",
    "securite":      "Sécurité",
    "fiabilite":     "Fiabilité",
}

# phrases d'état par catégorie : (bon ≥85, moyen ≥60, mauvais <60)
ETATS_CATEGORIE = {
    "performance":   ("Le site s'affiche rapidement.", "Le site pourrait être nettement plus rapide.",
                      "Le site est lent : vous perdez des visiteurs avant même qu'ils vous lisent."),
    "seo":           ("Le site est bien préparé pour Google.", "Google ne voit pas tout votre contenu correctement.",
                      "Le site est mal préparé pour Google : vos concurrents passent devant."),
    "accessibilite": ("Le site est accessible au plus grand nombre.", "Certains visiteurs sont gênés pour utiliser le site.",
                      "Une partie des visiteurs ne peut pas utiliser le site correctement."),
    "responsive":    ("Le site s'affiche bien sur téléphone.", "L'affichage sur téléphone mérite des corrections.",
                      "Le site s'affiche mal sur téléphone — la majorité de vos visiteurs."),
    "securite":      ("La connexion est correctement sécurisée.", "Des réglages de sécurité sont à compléter.",
                      "La sécurité du site présente des manques sérieux."),
    "fiabilite":     ("Aucune erreur bloquante détectée.", "Quelques erreurs techniques à surveiller.",
                      "Des erreurs empêchent des parties du site de fonctionner."),
}


def _verdict_global(score, n_prioritaires: int = 0) -> tuple[str, str, str]:
    """(niveau css, titre, phrase). Un bon score avec plusieurs priorités reste « à corriger »."""
    if score is None:
        return ("moyen", "Diagnostic partiel", "Toutes les familles de tests n'ont pas été mesurées.")
    if score >= 90 and n_prioritaires < 2:
        return ("bon", "Votre site est en très bon état",
                "Bonne base ! Les points ci-dessous vous feraient encore gagner en visibilité et en confort pour vos clients.")
    if score >= 75:
        return ("bon", "Votre site est en bon état, avec des points à corriger",
                "L'essentiel fonctionne, mais certains points listés ci-dessous vous coûtent des visiteurs ou des places sur Google.")
    if score >= 55:
        return ("moyen", "Votre site a besoin d'une remise à niveau",
                "Plusieurs problèmes concrets font fuir des visiteurs ou vous pénalisent sur Google. Ils sont corrigeables.")
    return ("mauvais", "Votre site vous fait perdre des clients",
            "En l'état, le site décourage une partie de vos visiteurs et Google le classe mal. Une intervention est vraiment conseillée.")


def _traduire(problemes_globaux: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Regroupe et traduit. Renvoie (prioritaires, conseillés, autres_bruts)."""
    groupes: dict[str, dict] = {}
    autres: list[str] = []
    for pb in problemes_globaux:
        msg, sev = pb.get("exemple", ""), pb.get("severite", "mineur")
        occ = pb.get("occurrences", 1)
        if sev == "info" and not re.search(r"donnée structurée|JSON-LD", msg):
            continue  # le bruit informatif reste dans le rapport technique
        if _IGNORER.search(msg):
            continue
        for rx, cle, titre, impact in _REGLES_COMPILEES:
            if rx.search(msg):
                g = groupes.setdefault(cle, {
                    "titre": titre, "impact": impact, "severites": [], "occurrences": 0, "bruts": [],
                })
                g["severites"].append(sev)
                g["occurrences"] += occ
                g["bruts"].append(msg)
                break
        else:
            autres.append(msg + (f" (×{occ})" if occ > 1 else ""))

    prioritaires, conseils = [], []
    for g in groupes.values():
        pire = min(g["severites"], key=lambda s: {"critique": 0, "majeur": 1, "mineur": 2, "info": 3}[s])
        g["pire"] = pire
        (prioritaires if pire in ("critique", "majeur") else conseils).append(g)
    tri = lambda g: ({"critique": 0, "majeur": 1, "mineur": 2, "info": 3}[g["pire"]], -g["occurrences"])
    prioritaires.sort(key=tri)
    conseils.sort(key=tri)
    return prioritaires, conseils, autres


def _capture_accueil_b64(rapport: dict, dossier: Path | None) -> str | None:
    """Capture de la 1re page, inline en base64 (rapport autonome). None si trop lourde/absente."""
    if not dossier:
        return None
    for pg in rapport.get("pages", []):
        rel = pg.get("capture")
        if rel:
            f = dossier / rel
            if f.is_file() and f.stat().st_size <= 3_500_000:
                b64 = base64.b64encode(f.read_bytes()).decode("ascii")
                return f"data:image/png;base64,{b64}"
            return None
    return None


def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================
#  GABARIT
# ============================================================

_CSS = r"""
  :root{--enc:#1c2733;--doux:#5c6b7a;--bord:#e3e8ee;--fond:#f6f8fa;--acc:#0b6bcb;
        --bon:#1a7f4b;--moyen:#b26a00;--mauvais:#c0392b;--carte:#ffffff;}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;color:var(--enc);background:var(--fond)}
  .wrap{max-width:820px;margin:0 auto;padding:36px 26px 60px}
  header{border-bottom:3px solid var(--acc);padding-bottom:18px;margin-bottom:26px}
  h1{font-size:26px;margin:0 0 2px} h2{font-size:19px;margin:34px 0 12px}
  .doux{color:var(--doux)} .petit{font-size:13px}
  .carte{background:var(--carte);border:1px solid var(--bord);border-radius:12px;padding:18px 20px;margin:12px 0;
         break-inside:avoid}
  .verdict{display:flex;gap:20px;align-items:center}
  .note{min-width:92px;height:92px;border-radius:50%;display:grid;place-items:center;
        font-size:30px;font-weight:800;color:#fff}
  .n-bon{background:var(--bon)} .n-moyen{background:var(--moyen)} .n-mauvais{background:var(--mauvais)}
  .verdict h2{margin:0 0 4px}
  .themes{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
  .theme{background:var(--carte);border:1px solid var(--bord);border-radius:10px;padding:12px 14px;break-inside:avoid}
  .theme .t{display:flex;justify-content:space-between;font-weight:700;margin-bottom:4px}
  .s-bon{color:var(--bon)} .s-moyen{color:var(--moyen)} .s-mauvais{color:var(--mauvais)}
  .point{border-left:4px solid var(--moyen);padding:10px 16px;margin:10px 0;background:var(--carte);
         border-radius:0 10px 10px 0;border-top:1px solid var(--bord);border-right:1px solid var(--bord);
         border-bottom:1px solid var(--bord);break-inside:avoid}
  .point.crit{border-left-color:var(--mauvais)}
  .point.min{border-left-color:var(--acc)}
  .point b{display:block;margin-bottom:2px}
  .badge{font-size:12px;font-weight:700;padding:1px 9px;border-radius:20px;vertical-align:middle;margin-left:8px}
  .b-n{background:#fdecea;color:var(--mauvais)}
  ol.essentiel{margin:0;padding-left:22px} ol.essentiel li{margin:10px 0}
  .apercu{max-height:440px;overflow:hidden;border:1px solid var(--bord);border-radius:12px}
  .apercu img{width:100%;display:block}
  .cta{background:#eaf3fd;border:1px solid #c8dff5;border-radius:12px;padding:20px 22px;margin-top:30px;break-inside:avoid}
  .cta h2{margin-top:0}
  .contact{margin-top:10px;font-weight:600}
  .contact span{display:inline-block;margin-right:22px}
  footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--bord)}
  ul.autres{columns:2;font-size:12.5px;color:var(--doux);margin:6px 0 0;padding-left:18px}
  @media print{
    body{background:#fff} .wrap{padding:0;max-width:none}
    .apercu{max-height:300px}
    a{color:inherit;text-decoration:none}
  }
  @page{size:A4;margin:16mm}
"""


def generer_rapport_client(rapport: dict, chemin: Path,
                           prestataire: dict | None = None,
                           dossier: Path | None = None) -> Path:
    """Écrit le rapport client autonome (HTML clair, imprimable) et renvoie son chemin."""
    meta = rapport.get("meta", {})
    scores = rapport.get("scores", {}) or {}
    domaine = meta.get("domaine", "site")
    date = (meta.get("date") or "").split(" ")[0]
    global_ = scores.get("global")

    prioritaires, conseils, autres = _traduire(rapport.get("problemes_globaux", []))
    niveau, titre_v, phrase_v = _verdict_global(global_, len(prioritaires))

    # liens cassés → un point client de plus (prioritaire dès 3 liens morts)
    casses = rapport.get("liens_casses", []) or []
    if casses:
        exemples = ", ".join(l["cible"] for l in casses[:3])
        item = {"titre": f"{len(casses)} lien(s) du site mènent à une page d'erreur",
                "impact": "Cliquer sur un lien mort casse la confiance et fait fuir — et Google le remarque aussi. "
                          f"Exemple(s) : {exemples}",
                "pire": "majeur" if len(casses) >= 3 else "mineur", "occurrences": len(casses), "bruts": []}
        (prioritaires if len(casses) >= 3 else conseils).append(item)

    # --- HTML ---
    h = []
    h.append(f"""<header>
      <h1>Diagnostic de votre site web</h1>
      <div class="doux" style="font-size:17px">{_esc(domaine)}</div>
      <div class="doux petit">Réalisé le {_esc(date)} · {meta.get('pages_auditees', '?')} page(s) analysée(s)
      {'· testé aussi en connexion lente' if meta.get('lent') else ''}</div>
    </header>""")

    note_txt = f"{global_}<span style='font-size:14px'>/100</span>" if global_ is not None else "–"
    h.append(f"""<div class="carte verdict">
      <div class="note n-{niveau}">{note_txt}</div>
      <div><h2>{_esc(titre_v)}</h2><div class="doux">{_esc(phrase_v)}</div></div>
    </div>""")

    # l'essentiel en 3 points (ou moins, s'il y a moins de priorités)
    if prioritaires:
        n = min(3, len(prioritaires))
        titre_ess = "L'essentiel en 3 points" if n == 3 else ("L'essentiel en 2 points" if n == 2 else "Le point essentiel")
        h.append(f"<h2>{titre_ess}</h2><div class='carte'><ol class='essentiel'>")
        for g in prioritaires[:3]:
            h.append(f"<li><b>{_esc(g['titre'])}.</b> <span class='doux'>{_esc(g['impact'])}</span></li>")
        h.append("</ol></div>")

    # notes par thème
    h.append("<h2>Vue d'ensemble</h2><div class='themes'>")
    for cat, label in CATEGORIES_CLIENT.items():
        v = scores.get(cat)
        if v is None:
            continue
        cls = "bon" if v >= 85 else ("moyen" if v >= 60 else "mauvais")
        bon, moyen, mauvais = ETATS_CATEGORIE[cat]
        phrase = bon if v >= 85 else (moyen if v >= 60 else mauvais)
        h.append(f"""<div class="theme"><div class="t"><span>{_esc(label)}</span>
          <span class="s-{cls}">{v}/100</span></div>
          <div class="doux petit">{_esc(phrase)}</div></div>""")
    h.append("</div>")

    # détail
    def bloc_points(items, classe):
        for g in items:
            occ = f"<span class='badge b-n'>relevé {g['occurrences']} fois</span>" if g["occurrences"] > 1 else ""
            h.append(f"""<div class="point {classe}"><b>{_esc(g['titre'])}{occ}</b>
              <span class="doux">{_esc(g['impact'])}</span></div>""")

    if prioritaires:
        h.append(f"<h2>À corriger en priorité ({len(prioritaires)})</h2>")
        bloc_points(prioritaires, "crit")
    if conseils:
        h.append(f"<h2>Améliorations conseillées ({len(conseils)})</h2>")
        bloc_points(conseils, "min")
    if not prioritaires and not conseils:
        h.append("<h2>Points relevés</h2><div class='carte'>Rien de bloquant n'a été détecté "
                 "sur les pages analysées — félicitations, c'est rare.</div>")

    # capture
    img = _capture_accueil_b64(rapport, dossier)
    if img:
        h.append(f"""<h2>Votre page d'accueil vue par l'outil</h2>
          <div class="apercu"><img src="{img}" alt="Capture d'écran de la page d'accueil de {_esc(domaine)}"></div>""")

    # CTA prestataire
    p = prestataire or {}
    if p:
        contact = "".join(
            f"<span>{_esc(icone)} {_esc(val)}</span>"
            for icone, val in (("👤", p.get("nom")), ("✉️", p.get("email")),
                               ("📞", p.get("telephone")), ("🌐", p.get("site")))
            if val)
        h.append(f"""<div class="cta"><h2>Et maintenant ?</h2>
          Ce diagnostic vous est offert. Chaque point listé ci-dessus se corrige — la plupart en quelques jours.
          Je peux m'en charger, ou refaire votre site sur une base moderne, rapide et bien référencée.
          <div class="contact">{contact}</div>
          {f"<div class='doux petit' style='margin-top:6px'>{_esc(p['titre'])}</div>" if p.get('titre') else ''}
        </div>""")

    # autres points techniques
    if autres:
        h.append(f"""<footer><div class="petit doux"><b>Autres points techniques relevés</b>
          (détail destiné à votre prestataire) :</div>
          <ul class="autres">{''.join(f'<li>{_esc(a)}</li>' for a in autres[:14])}</ul>
          {f"<div class='petit doux'>… et {len(autres) - 14} autres points dans le rapport technique.</div>" if len(autres) > 14 else ''}
        </footer>""")

    h.append("""<footer class="petit doux">Diagnostic automatisé (performance, Google, mobile, sécurité,
      accessibilité, fiabilité) réalisé avec un outil d'audit professionnel. Les mesures reflètent
      l'état du site au jour indiqué. Un rapport technique détaillé est disponible sur demande.</footer>""")

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diagnostic web — {_esc(domaine)}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">{''.join(h)}</div></body></html>"""
    chemin.write_text(html, encoding="utf-8")
    return chemin
