#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interface web locale de l'auditeur.

Lance un petit serveur sur la machine, ouvre le navigateur, demande l'adresse
du site à tester, exécute `auditeur.py` en affichant la progression EN DIRECT,
puis affiche le rapport HTML. Pensé pour un destinataire NON technicien : il
suffit de lancer ce script (ou le bouton « Lancer l'auditeur ») et de saisir une URL.

Lancement : python interface.py   (le navigateur s'ouvre tout seul)
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

for _f in (sys.stdout, sys.stderr):
    try:
        _f.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from flask import Flask, Response, request, send_from_directory, jsonify
except ImportError:
    sys.exit("❌ Flask manquant. Installe les dépendances :  pip install -r requirements.txt")

RACINE = Path(__file__).resolve().parent
AUDITEUR = RACINE / "auditeur.py"
RAPPORTS = RACINE / "rapports"
RAPPORTS.mkdir(exist_ok=True)
SCENARIOS = RACINE / "scenarios"


def _hote(u: str) -> str:
    h = (urlparse(u if "://" in u else "https://" + u).netloc or "").lower()
    return h.removeprefix("www.")


def scenarios_pour(url: str) -> list[Path]:
    """Parcours (scénarios) du dossier scenarios/ dont l'URL pointe sur le même domaine."""
    cible = _hote(url)
    trouves = []
    if cible and SCENARIOS.is_dir():
        for f in sorted(SCENARIOS.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(d, dict) and d.get("url") and _hote(d["url"]) == cible:
                trouves.append(f)
    return trouves

app = Flask(__name__, static_folder=None)


# ============================================================
#  PAGE D'ACCUEIL (formulaire + progression + lien rapport)
# ============================================================

PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auditeur web — tester un site</title>
<style>
  :root{--bg:#0f1115;--carte:#1a1d24;--txt:#e6e8eb;--doux:#9aa3ad;--bord:#2a2f3a;
        --acc:#5ab0ff;--ok:#3ddc84;--ko:#ff5d5d;--maj:#ffa23e;}
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
  .wrap{max-width:760px;margin:0 auto;padding:40px 22px 80px}
  h1{font-size:26px;margin:0 0 6px}
  .doux{color:var(--doux)}
  .carte{background:var(--carte);border:1px solid var(--bord);border-radius:14px;padding:22px;margin-top:22px}
  label{display:block;font-weight:600;margin-bottom:6px}
  input[type=text]{width:100%;padding:14px 16px;font-size:17px;border-radius:10px;border:1px solid var(--bord);
    background:#0c0e12;color:var(--txt)}
  input[type=text]:focus{outline:none;border-color:var(--acc)}
  .opts{display:flex;flex-wrap:wrap;gap:18px;margin-top:18px;align-items:center}
  .opt{display:flex;align-items:center;gap:8px;color:var(--doux);font-size:14px}
  .opt input{width:18px;height:18px}
  .num{width:74px;padding:8px;border-radius:8px;border:1px solid var(--bord);background:#0c0e12;color:var(--txt)}
  button{margin-top:22px;width:100%;padding:15px;font-size:17px;font-weight:700;border:none;border-radius:10px;
    background:var(--acc);color:#06121f;cursor:pointer}
  button:disabled{opacity:.5;cursor:progress}
  .progress{display:none;margin-top:22px}
  pre{background:#06080c;border:1px solid var(--bord);border-radius:10px;padding:14px;max-height:330px;
    overflow:auto;font:13px/1.5 ui-monospace,Consolas,monospace;color:#cbd3dc;white-space:pre-wrap}
  .fini{display:none;margin-top:18px;text-align:center}
  .fini a{display:inline-block;padding:14px 26px;background:var(--ok);color:#04140a;font-weight:700;
    border-radius:10px;text-decoration:none;font-size:17px}
  .err{color:var(--ko)}
  .pied{margin-top:30px;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔍 Auditeur web</h1>
  <div class="doux">Teste n'importe quel site : performance, SEO, accessibilité, mobile, sécurité,
    liens cassés… et te donne un rapport clair avec les problèmes priorisés.</div>

  <div class="carte">
    <label for="url">Adresse du site à tester</label>
    <input id="url" type="text" placeholder="exemple.com  ou  https://monsite.com" autofocus
           autocomplete="off" spellcheck="false">
    <div class="opts">
      <span class="opt"><label style="margin:0">Pages à explorer</label>
        <input id="pages" class="num" type="number" min="1" max="100" value="15"></span>
      <span class="opt"><input id="mobile" type="checkbox"> Vue mobile</span>
      <span class="opt"><input id="lent" type="checkbox"> Connexion lente (3G)</span>
      <span class="opt"><input id="parcours" type="checkbox" checked> Explorer les parcours automatiquement (inscription, formulaires…)</span>
    </div>
    <button id="go">Lancer l'audit</button>

    <div class="progress" id="progress">
      <div class="doux" id="etat">⏳ Audit en cours…</div>
      <pre id="log"></pre>
    </div>
    <div class="fini" id="fini">
      <a id="lienRapport" href="#" target="_blank">📄 Ouvrir le rapport</a>
      <div class="doux" style="margin-top:10px"><a href="#" id="recommencer" style="color:var(--acc)">↻ Tester un autre site</a></div>
    </div>
  </div>

  <div class="pied doux">
    L'audit s'exécute <b>entièrement sur cette machine</b> ; rien n'est envoyé ailleurs.
    Plus il y a de pages, plus c'est long. Tu peux fermer la fenêtre noire pour tout arrêter.
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const go = $('go'), log = $('log'), prog = $('progress'), fini = $('fini'), etat = $('etat');
let source = null;

function lancer() {
  const url = $('url').value.trim();
  if (!url) { $('url').focus(); return; }
  go.disabled = true; fini.style.display = 'none'; prog.style.display = 'block';
  log.textContent = ''; etat.textContent = '⏳ Démarrage…'; etat.className = 'doux';

  const p = new URLSearchParams({
    url, pages: $('pages').value || '15',
    mobile: $('mobile').checked ? '1' : '0',
    lent: $('lent').checked ? '1' : '0',
    auto: $('parcours').checked ? '1' : '0',
  });
  source = new EventSource('/auditer?' + p.toString());
  source.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.ligne != null) {
      log.textContent += d.ligne + '\n'; log.scrollTop = log.scrollHeight;
      if (d.ligne.includes('Score global')) etat.textContent = '✅ Presque fini…';
    }
    if (d.fini) {
      source.close();
      if (d.rapport) {
        etat.textContent = '✅ Audit terminé !'; etat.className = 'ok';
        $('lienRapport').href = d.rapport; fini.style.display = 'block';
        window.open(d.rapport, '_blank');
      } else {
        etat.textContent = '❌ ' + (d.erreur || "L'audit a échoué. Vérifie l'adresse.");
        etat.className = 'err';
      }
      go.disabled = false;
    }
  };
  source.onerror = () => {
    if (source) source.close();
    etat.textContent = '❌ Connexion au serveur interrompue.'; etat.className = 'err';
    go.disabled = false;
  };
}

go.addEventListener('click', lancer);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') lancer(); });
$('recommencer').addEventListener('click', e => {
  e.preventDefault(); fini.style.display = 'none'; prog.style.display = 'none';
  $('url').value = ''; $('url').focus();
});
</script>
</body>
</html>
"""


def _slug(url: str) -> str:
    dom = urlparse(url if "://" in url else "https://" + url).netloc or "site"
    dom = re.sub(r"[^A-Za-z0-9._-]", "-", dom)
    return f"{dom}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


@app.route("/")
def accueil():
    return PAGE


@app.route("/auditer")
def auditer():
    """Lance auditeur.py et diffuse sa sortie ligne par ligne (Server-Sent Events)."""
    url = (request.args.get("url") or "").strip()
    if not url:
        return Response("data: " + json.dumps({"fini": True, "erreur": "Adresse vide"}) + "\n\n",
                        mimetype="text/event-stream")

    try:
        pages = max(1, min(100, int(request.args.get("pages", "15"))))
    except ValueError:
        pages = 15
    mobile = request.args.get("mobile") == "1"
    lent = request.args.get("lent") == "1"
    auto = request.args.get("auto", "1") == "1"

    dossier = RAPPORTS / _slug(url)
    cmd = [sys.executable, "-u", str(AUDITEUR), url,
           "--max-pages", str(pages), "--sortie", str(dossier)]
    if mobile:
        cmd.append("--mobile")
    if lent:
        cmd.append("--lent")
    # Profil pour remplir les formulaires (jamais soumis depuis l'interface) :
    # le profil perso s'il existe, sinon le profil de test neutre livré avec l'outil.
    for nom_profil in ("profil.json", "profil.test.json"):
        chemin = RACINE / nom_profil
        if chemin.is_file():
            cmd += ["--profil", str(chemin)]
            break
    # Parcours EN PLUS de l'audit générique : exploration auto + scénarios explicites du domaine.
    fichiers_sc = scenarios_pour(url)  # bonus si un JSON existe pour ce domaine (sinon : aucun)
    if auto or fichiers_sc:
        cmd.append("--avec-audit")
    if auto:
        cmd.append("--auto")
    for f in fichiers_sc:
        cmd += ["--scenario", str(f)]

    def flux():
        yield "retry: 3000\n\n"
        if auto:
            yield "data: " + json.dumps(
                {"ligne": "🧭 Exploration automatique des parcours activée (remplissage + clics « Continuer »)."},
                ensure_ascii=False) + "\n\n"
        if fichiers_sc:
            noms = ", ".join(f.stem for f in fichiers_sc)
            yield "data: " + json.dumps(
                {"ligne": f"🎬 {len(fichiers_sc)} scénario(s) du domaine aussi rejoué(s) : {noms}"},
                ensure_ascii=False) + "\n\n"
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", cwd=str(RACINE), bufsize=1)
        except Exception as e:
            yield "data: " + json.dumps({"fini": True, "erreur": f"Lancement impossible : {e}"}) + "\n\n"
            return
        for ligne in proc.stdout:
            ligne = ligne.rstrip("\n")
            if ligne:
                yield "data: " + json.dumps({"ligne": ligne}, ensure_ascii=False) + "\n\n"
        proc.wait()

        # index.html (mode combiné audit + parcours) en priorité, sinon le rapport simple
        cible = next((nom for nom in ("index.html", "rapport.html")
                      if (dossier / nom).is_file()), None)
        if cible:
            rel = "/rapports/" + dossier.name + "/" + cible
            yield "data: " + json.dumps({"fini": True, "rapport": rel}) + "\n\n"
        else:
            yield "data: " + json.dumps({
                "fini": True,
                "erreur": "Aucun rapport généré — l'adresse est peut-être injoignable.",
            }) + "\n\n"

    return Response(flux(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/rapports/<path:chemin>")
def voir_rapport(chemin):
    return send_from_directory(RAPPORTS, chemin)


# ============================================================
#  DÉMARRAGE : trouve un port libre, ouvre le navigateur
# ============================================================

def _port_libre(prefere=5000):
    for port in [prefere, 5001, 5002, 8080, 8000, 0]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            continue
    return 5000


def main():
    port = _port_libre()
    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print("  Auditeur web — interface prête")
    print(f"  → Ouvre ton navigateur sur : {url}")
    print("  (Garde cette fenêtre ouverte ; ferme-la pour arrêter.)")
    print("=" * 56)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # threaded=True : indispensable pour servir la progression (SSE) pendant l'audit.
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
