#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Banc d'essai multi-sites pour l'auditeur.

Lance `auditeur.py` sur une liste variée de sites (statique, WordPress, Shopify,
SPA React/Next, presse, e-commerce, administration…), en parallèle, puis dresse
un tableau comparatif. Sert surtout à VÉRIFIER LA ROBUSTESSE : on veut qu'aucun
type de site ne fasse planter l'outil ni ne produise un rapport vide.

Usage :
    python tester_lot.py                      # liste intégrée (≈ 16 sites)
    python tester_lot.py --sites mes.txt      # un domaine par ligne
    python tester_lot.py https://a.com https://b.com
    python tester_lot.py --max-pages 2 --parallele 3 --lent

Sortie :
    lot_<date>/<domaine>/...   (un rapport complet par site)
    lot_<date>/recap.md        (tableau comparatif lisible)
    lot_<date>/recap.json      (données brutes)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

for _f in (sys.stdout, sys.stderr):
    try:
        _f.reconfigure(encoding="utf-8")
    except Exception:
        pass

RACINE = Path(__file__).resolve().parent
AUDITEUR = RACINE / "auditeur.py"

# Échantillon volontairement hétérogène : chaque ligne = un type de site
# différent, pour éprouver l'outil sur le plus large spectre possible.
SITES_DEFAUT = [
    "https://example.com",                 # statique minimal
    "https://www.gov.uk",                  # administration, bannière cookies
    "https://react.dev",                   # SPA / docs React
    "https://nextjs.org",                  # Next.js
    "https://wordpress.org",               # WordPress
    "https://vuejs.org",                   # Vue
    "https://getbootstrap.com",            # site doc + Bootstrap
    "https://www.python.org",              # contenu riche, ancien
    "https://news.ycombinator.com",        # HTML brut, dense
    "https://www.wikipedia.org",           # portail multilingue
    "https://developer.mozilla.org",       # gros site de doc
    "https://www.bbc.com",                 # presse, lourd, trackers
    "https://tailwindcss.com",             # marketing + Tailwind
    "https://svelte.dev",                  # Svelte
    "https://astro.build",                 # Astro
    "https://www.shopify.com",             # SaaS e-commerce
]


def charger_sites(args) -> list[str]:
    sites: list[str] = []
    if args.fichier:
        for ligne in Path(args.fichier).read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#"):
                sites.append(ligne)
    sites += list(args.urls or [])
    if not sites:
        sites = list(SITES_DEFAUT)
    # normalise le schéma
    return [u if "://" in u else "https://" + u for u in sites]


def auditer_un(url: str, dossier: Path, args) -> dict:
    """Lance auditeur.py en sous-processus et renvoie un résumé exploitable."""
    dom = urlparse(url).netloc or url
    sortie = dossier / dom.replace(":", "_")
    cmd = [sys.executable, str(AUDITEUR), url,
           "--max-pages", str(args.max_pages),
           "--concurrence", str(args.concurrence),
           "--sortie", str(sortie)]
    if args.lent:
        cmd.append("--lent")
    if args.mobile:
        cmd.append("--mobile")
    if args.only:
        cmd += ["--only", args.only]

    debut = datetime.now()
    resume = {"url": url, "domaine": dom, "ok": False, "erreur": None,
              "duree_s": None, "scores": {}, "nb": {}, "stack": [], "liens_casses": 0}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        resume["erreur"] = f"timeout > {args.timeout}s"
        return resume
    except Exception as e:
        resume["erreur"] = f"lancement : {e}"
        return resume
    finally:
        resume["duree_s"] = round((datetime.now() - debut).total_seconds(), 1)

    rj = sortie / "rapport.json"
    if not rj.is_file():
        # on remonte la dernière ligne utile de stderr pour diagnostiquer
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        resume["erreur"] = f"pas de rapport (code {proc.returncode}) : {tail[-1] if tail else '?'}"
        return resume
    try:
        d = json.loads(rj.read_text(encoding="utf-8"))
    except Exception as e:
        resume["erreur"] = f"rapport illisible : {e}"
        return resume

    nb = {"critique": 0, "majeur": 0, "mineur": 0, "info": 0}
    for pb in d.get("problemes_globaux", []):
        nb[pb["severite"]] = nb.get(pb["severite"], 0) + pb.get("occurrences", 1)
    resume.update({
        "ok": True,
        "scores": d.get("scores", {}),
        "nb": nb,
        "stack": (d.get("meta", {}).get("site", {}) or {}).get("tech", []),
        "spa": (d.get("meta", {}).get("site", {}) or {}).get("spa", False),
        "pages": d.get("meta", {}).get("pages_auditees", 0),
        "liens_casses": len(d.get("liens_casses", [])),
        "dossier": str(sortie),
    })
    return resume


def ecrire_recap(resultats: list[dict], dossier: Path):
    (dossier / "recap.json").write_text(
        json.dumps(resultats, ensure_ascii=False, indent=2), encoding="utf-8")

    lignes = ["# Récapitulatif du banc d'essai", "",
              f"{len(resultats)} sites · {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
              "| Site | Global | Perf | SEO | A11y | Resp | Sécu | Fiab | Crit/Maj | Liens✗ | Stack | Durée |",
              "|------|:------:|:----:|:---:|:----:|:----:|:----:|:----:|:--------:|:------:|-------|------:|"]

    def c(v):
        return "–" if v is None else str(v)

    ok = ko = 0
    for r in sorted(resultats, key=lambda x: (not x["ok"], -(x.get("scores", {}).get("global") or 0))):
        if not r["ok"]:
            ko += 1
            lignes.append(f"| {r['domaine']} | ❌ **{r['erreur']}** | | | | | | | | | | {r['duree_s']}s |")
            continue
        ok += 1
        s = r["scores"]
        nb = r["nb"]
        stack = ", ".join(r.get("stack", [])[:3]) + (" (SPA)" if r.get("spa") else "")
        lignes.append(
            f"| {r['domaine']} | **{c(s.get('global'))}** | {c(s.get('performance'))} | "
            f"{c(s.get('seo'))} | {c(s.get('accessibilite'))} | {c(s.get('responsive'))} | "
            f"{c(s.get('securite'))} | {c(s.get('fiabilite'))} | "
            f"{nb.get('critique',0)}/{nb.get('majeur',0)} | {r['liens_casses']} | {stack} | {r['duree_s']}s |")

    lignes += ["", f"**{ok} réussis · {ko} en échec.**"]
    if ko:
        lignes.append("\n> ⚠️ Les échecs ci-dessus sont les sites à investiguer pour la robustesse.")
    (dossier / "recap.md").write_text("\n".join(lignes), encoding="utf-8")
    return ok, ko


def main():
    ap = argparse.ArgumentParser(description="Banc d'essai multi-sites de l'auditeur.")
    ap.add_argument("urls", nargs="*", help="sites à tester (sinon : liste intégrée)")
    ap.add_argument("--sites", dest="fichier", help="fichier : un site par ligne")
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--concurrence", type=int, default=4, help="concurrence INTERNE à chaque audit")
    ap.add_argument("--parallele", type=int, default=3, help="nombre d'audits menés en parallèle")
    ap.add_argument("--timeout", type=int, default=300, help="budget (s) par site")
    ap.add_argument("--lent", action="store_true", help="bride la connexion (bas débit) pour chaque audit")
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("--only", help="familles à tester (passé tel quel à l'auditeur)")
    ap.add_argument("--sortie", help="dossier de sortie (défaut : lot_<date>)")
    args = ap.parse_args()

    if not AUDITEUR.is_file():
        sys.exit(f"❌ auditeur.py introuvable à côté de ce script ({AUDITEUR})")

    sites = charger_sites(args)
    dossier = Path(args.sortie) if args.sortie else Path(f"lot_{datetime.now().strftime('%Y%m%d_%H%M')}")
    dossier.mkdir(parents=True, exist_ok=True)

    print(f"🧪 Banc d'essai : {len(sites)} sites · {args.parallele} en parallèle · "
          f"{args.max_pages} pages/site → {dossier}/\n")

    resultats: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.parallele) as ex:
        futs = {ex.submit(auditer_un, u, dossier, args): u for u in sites}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            resultats.append(r)
            if r["ok"]:
                g = r["scores"].get("global")
                print(f"  ✅ [{i}/{len(sites)}] {r['domaine']:<28} global={g} "
                      f"({r['nb'].get('critique',0)}c/{r['nb'].get('majeur',0)}m) {r['duree_s']}s")
            else:
                print(f"  ❌ [{i}/{len(sites)}] {r['domaine']:<28} {r['erreur']}")

    ok, ko = ecrire_recap(resultats, dossier)
    print(f"\n📊 {ok} réussis · {ko} en échec")
    print(f"📄 Récap : {dossier / 'recap.md'}")


if __name__ == "__main__":
    main()
