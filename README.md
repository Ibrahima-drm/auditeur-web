# Auditeur web

Robot de test automatique pour **n'importe quel type de site** (statique, WordPress,
Shopify, SPA React/Next/Vue, presse, e-commerce, administration…). Il explore le site
(crawl interne **+ amorçage par le sitemap**), **détecte la pile technique**, **ferme
les bannières cookies** pour auditer le vrai site, puis pour chaque page mesure la
performance, le SEO, l'accessibilité, le responsive, la sécurité, les liens et les
erreurs JavaScript. Il produit un rapport JSON complet, un **rapport HTML lisible**
(scores + problèmes priorisés) et des captures d'écran.

> Tester l'outil sur plein de sites d'un coup : `python tester_lot.py` (voir plus bas).

## 🟢 Démarrage en un clic (pour offrir l'outil à quelqu'un)

Aucune compétence technique requise pour la personne qui reçoit le dossier :

1. **Windows** — double-clic sur **`Lancer l'auditeur.bat`**
   **Mac** — double-clic sur **`Lancer l'auditeur.command`** (clic droit → *Ouvrir* la 1re fois)
2. Au tout premier lancement, il **installe automatiquement** ce qu'il faut
   (environnement Python, Playwright, navigateur Chromium ≈ 150 Mo) — patiente quelques minutes.
3. Une **page s'ouvre dans le navigateur** : il suffit d'y coller l'**adresse du site** et de
   cliquer sur **« Lancer l'audit »**. La progression s'affiche en direct, puis le **rapport
   s'ouvre tout seul**.

Tout tourne **sur la machine de la personne** ; rien n'est envoyé sur Internet (à part la
visite normale du site testé). Les lancements suivants sont quasi instantanés.

> 📦 **Avant d'envoyer le dossier** : supprime **`profil.json`** (il contient tes infos
> perso pour les formulaires) et les anciens dossiers `rapport_*`/`rapports/`. Tu peux
> zipper le reste et l'envoyer tel quel.
>
> ⚠️ Si Windows dit que Python est introuvable, le lanceur tente de l'installer puis demande
> de **fermer et relancer** le fichier (le temps que Windows prenne en compte l'installation).

## Installation (manuelle, si tu préfères la ligne de commande)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Utilisation

```bash
# audit complet (valeurs par défaut : 30 pages, profondeur 3)
python auditeur.py https://monsite.com

# plus de pages, plus rapide
python auditeur.py https://monsite.com --max-pages 80 --concurrence 8

# une seule famille de tests
python auditeur.py https://monsite.com --only seo,performance

# émulation mobile
python auditeur.py https://monsite.com --mobile

# expérience BAS DÉBIT (~3G) : crucial pour les connexions faibles
python auditeur.py https://monsite.com --mobile --lent

# site qui demande une connexion (session Playwright sauvegardée)
python auditeur.py https://monsite.com --auth storage_state.json

# rapport CLIENT en plus (diagnostic commercial, voir plus bas)
python auditeur.py https://monsite.com --client
```

## Rapport client (prospection freelance)

`--client` génère **`rapport-client.html`** en plus du rapport technique : un
diagnostic **sans jargon**, pensé pour être envoyé tel quel à un commerçant /
artisan. Chaque constat technique y est traduit en conséquence concrète
(« visiteurs perdus », « Google vous classe moins bien », « perte de
confiance »), avec une note globale, l'essentiel en 3 points, les priorités,
la capture de la page d'accueil **incluse dans le fichier** (autonome, s'envoie
par email) et un bloc contact. Il s'imprime proprement en PDF (Ctrl+P).

```bash
# audit + rapport client (coordonnées lues dans prestataire.json s'il existe)
python auditeur.py https://commerce.fr --client

# coordonnées explicites
python auditeur.py https://commerce.fr --client --prestataire moi.json

# (re)générer SEULEMENT le rapport client depuis un audit déjà fait (sans re-crawler)
python auditeur.py --client-depuis rapport_commerce.fr_20260702_1010/rapport.json
```

Copie `prestataire.exemple.json` en `prestataire.json` avec tes coordonnées
(nom, titre, email, téléphone, site) — ignoré par git. L'interface web propose
aussi une case « **Rapport client** ».

## Tester les formulaires avec tes vraies infos (et créer un compte)

1. Copie `profil.exemple.json` en `profil.json` et mets tes infos (nom, email, mot de passe…).
   `profil.json` est ignoré par git (jamais committé).
2. Remplissage **sans** soumettre (sans risque) — voir le rendu/la validation :
   ```bash
   python auditeur.py https://monsite.com --profil profil.json
   ```
3. Soumission **réelle** (crée de vrais comptes / envoie de vrais messages) :
   ```bash
   # sur localhost / staging : autorisé directement
   python auditeur.py http://localhost:3000 --profil profil.json --soumettre-formulaires
   # sur un vrai domaine (prod) : garde-fou, il faut l'autoriser explicitement
   python auditeur.py https://monsite.com --profil profil.json --soumettre-formulaires --autoriser-prod
   ```

L'outil reconnaît automatiquement les champs (prénom, nom, email, téléphone, mot de
passe **+ confirmation**, adresse, ville, pays, message…), coche les **CGU**, laisse
les cases **newsletter** décochées, choisit le **pays** dans les listes déroulantes,
puis détecte le résultat (redirection, message d'erreur, succès probable). Les mots
de passe sont **masqués** dans le rapport.

> ⚠️ La soumission réelle est **bloquée par défaut sur un vrai domaine** : il faut
> `--autoriser-prod`. Préfère tester sur `localhost`/`staging` pour ne pas polluer
> ta base de prod (vrais comptes, emails, captcha…). Astuce : `--headful` pour
> voir le remplissage en direct.

## Options principales

| Option | Effet |
|--------|-------|
| `--max-pages N` | nombre max de pages explorées (défaut 30) |
| `--max-depth N` | profondeur max du crawl (défaut 3) |
| `--concurrence N` | pages auditées en parallèle (défaut 4) |
| `--only fam,fam` | familles : `performance, seo, accessibilite, responsive, securite, liens, formulaires` |
| `--mobile` | émule un mobile (viewport + flag mobile) |
| `--lent` | bride la connexion (~3G : 400 kbps, CPU ×4) pour tester le bas débit |
| `--garder-cookies` | ne **pas** fermer les bannières de consentement (fermées par défaut) |
| `--sous-domaines` | crawle aussi les sous-domaines |
| `--inclure REGEX` / `--exclure REGEX` | filtre les URLs crawlées |
| `--ignorer-robots` | ne respecte pas `robots.txt` (respecté par défaut) |
| `--soumettre-formulaires` | ⚠️ soumet **réellement** les formulaires |
| `--auth fichier.json` | charge une session connectée (`storage_state`) |
| `--sortie DOSSIER` | dossier de sortie |
| `--client` | génère aussi `rapport-client.html` (diagnostic commercial sans jargon) |
| `--prestataire F.json` | coordonnées affichées dans le rapport client (défaut : `prestataire.json`) |
| `--client-depuis R.json` | régénère le rapport client depuis un `rapport.json` existant |

## Ce qui est testé

- **Au niveau du site (une fois)** — pile technique détectée (React, Next, Vue, Angular,
  WordPress, Shopify, Wix… + trackers GA/Pixel/Hotjar), redirection `http→https`,
  canonicalisation `www`/apex, **soft-404** (URL bidon renvoyant 200), présence d'un
  **sitemap** (qui sert aussi à amorcer le crawl).
- **Performance** — Core Web Vitals (LCP, CLS, FCP, **TTFB signalé**), temps de
  chargement, poids transféré, requêtes ; **analyse des images** (sur-dimensionnées =
  poids gâché, sans `width/height` = sauts de page, cassées, lazy-loading) ; **compression**
  gzip/brotli manquante ; **cache** des actifs statiques ; **render-blocking** CSS/JS.
- **SEO** — title, méta description, h1, hiérarchie des titres, viewport, canonical,
  lang, Open Graph, JSON-LD, images sans `alt`, **hreflang / i18n**, manifeste PWA.
- **Accessibilité** — analyse **axe-core** (violations par impact, dont le contraste)
  + checks manuels (labels, boutons/liens sans nom, lang) + **zoom désactivé**
  (`user-scalable=no`).
- **Responsive & tactile** — débordement horizontal mobile/tablette/bureau avec les
  **éléments coupables**, **cibles tactiles trop petites** (<24px) et **polices < 12px**.
- **Sécurité** — en-têtes (HSTS, CSP, X-Frame-Options…), cookies non sécurisés,
  contenu mixte HTTP, `target=_blank` sans `rel=noopener`.
- **Fiabilité** — erreurs JavaScript, erreurs console, requêtes 4xx/5xx, **contenu de
  remplissage** oublié (lorem ipsum, « coming soon »…).
- **Liens** — vérification HTTP, en distinguant **liens cassés** (404, 5xx, erreur
  réseau) et **liens restreints** (anti-bot / rate-limit / connexion requise : 403,
  429, plateformes sociales) qui ne sont **pas** comptés comme cassés.
- **Formulaires** — détection + remplissage automatique (soumission optionnelle).

## Banc d'essai multi-sites

Pour éprouver l'outil sur un large échantillon (et vérifier qu'aucun type de site ne
le fait planter) :

```bash
python tester_lot.py                       # liste intégrée hétérogène (≈ 16 sites)
python tester_lot.py https://a.com https://b.com
python tester_lot.py --sites mes_sites.txt # un domaine par ligne
python tester_lot.py --max-pages 2 --parallele 4 --lent
```

Sortie : un dossier `lot_<date>/` avec un rapport complet par site **plus** un
**tableau comparatif** `recap.md` (scores par catégorie, problèmes, pile, liens
cassés, durée) et `recap.json`.

## Mode scénario (parcours multi-étapes : inscription, tunnel…)

Pour tester un **tunnel d'inscription en plusieurs écrans** (que l'audit normal ne
sait pas traverser), décris le parcours dans un fichier JSON et l'outil le rejoue en
te disant **exactement à quelle étape ça casse**, avec une capture de l'écran bloquant.

```bash
python auditeur.py --scenario scenarios/ibi-inscription.json
```

Exemple (`scenarios/ibi-inscription.json`) :

```json
{
  "nom": "IBI Smart School — inscription",
  "url": "https://ibi-smartschool.com/start",
  "etapes": [
    {"action": "cliquer", "texte": "Créer mon compte"},
    {"action": "cliquer", "texte": "Mode Standard"},
    {"action": "remplir", "placeholder": "Ton prénom", "valeur": "Ibrahima"},
    {"action": "cliquer", "texte": "Continuer"},
    {"action": "remplir", "selecteur": "input[type=date]", "valeur": "2008-05-15"},
    {"action": "verifier", "bouton_actif": "Continuer",
     "decrire": "Le bouton doit s'activer après une date valide"}
  ]
}
```

Actions disponibles : `aller`, `cliquer` (par `texte` ou `selecteur`), `remplir`
(`valeur` ou `profil`), `choisir`, `cocher`, `presser`, `attendre`, `capture`, et
`verifier` (`bouton_actif`, `bouton_inactif`, `texte_present`, `url_contient`,
`visible`). Sortie : `scenario.html` (timeline avec captures) + `scenario.json`.

## Sorties

```
rapport_<domaine>_<date>/
├── rapport.json         # toutes les données brutes
├── rapport.html         # rapport lisible avec scores et problèmes priorisés
├── rapport-client.html  # (avec --client) diagnostic commercial autonome
└── captures/            # captures pleine page de chaque URL
```

## Score

Chaque catégorie part de 100 ; chaque problème retranche selon sa gravité
(critique −25, majeur −10, mineur −4). Le score global est la moyenne des catégories.
