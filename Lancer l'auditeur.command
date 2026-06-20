#!/usr/bin/env bash
# Lanceur Mac / Linux. Sur Mac : double-clic (clic droit > Ouvrir la 1re fois).
# Si « permission refusée », exécute une fois :  chmod +x "Lancer l'auditeur.command"
cd "$(dirname "$0")" || exit 1

echo "============================================"
echo "    AUDITEUR WEB  -  démarrage"
echo "============================================"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 n'est pas installé."
  echo "Installe-le depuis https://www.python.org/downloads/ puis relance ce fichier."
  read -r -p "Appuie sur Entrée pour fermer..."
  exit 1
fi

echo "Python détecté : $(python3 --version)"
echo

[ -d .venv ] || { echo "Création de l'environnement (première fois)..."; python3 -m venv .venv; }
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.pret ]; then
  echo
  echo "Installation des composants (Playwright + Chromium, ~150 Mo la 1re fois)..."
  echo
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt || { echo "Échec de l'installation."; read -r -p "Entrée..."; exit 1; }
  python -m playwright install chromium || { echo "Échec du navigateur."; read -r -p "Entrée..."; exit 1; }
  touch .venv/.pret
fi

echo
echo "Ouverture de l'interface dans ton navigateur..."
echo
python interface.py

read -r -p "Interface fermée. Appuie sur Entrée pour fermer..."
