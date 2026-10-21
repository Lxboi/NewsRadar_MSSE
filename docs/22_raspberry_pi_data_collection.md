# Déporter la collecte de données sur le Raspberry Pi (guide pratique)

Comment transférer `crypto_radar/` sur le Raspberry Pi, y faire tourner
`python -m src.main` en continu pendant plusieurs jours pour la collecte
(RESEARCH mode — aucune clé API requise), puis rapatrier les données
collectées vers le PC pour y faire le training/paper trading (plus
lourd en calcul). Ce guide ne couvre que la collecte — pas un
déploiement 24/7 complet avec monitoring de dérive d'horloge/watchdog
avancé (ça reste la Phase 17, non construite).

## 1. Prérequis sur le Pi

- Raspberry Pi OS 64-bit (recommandé, cohérent avec le Pi 5 4GB déjà
  pris en compte lors du choix des sources RSS en Phase 5).
- Python : `requirements.txt`/`README.md` recommandent 3.12.10
  exactement, mais **aucune syntaxe spécifique à 3.12 n'est utilisée
  dans le code** (vérifié par grep) et la suite de tests complète (719
  tests) passe sans problème sous Python 3.11 dans cet environnement de
  build. Raspberry Pi OS Bookworm fournit Python 3.11 par défaut — ça
  devrait suffire pour cette collecte. Vérifie avec `python3 --version`
  ; si tu tombes sur une erreur de syntaxe liée à la version, on
  installera 3.12 via `pyenv` à ce moment-là, pas la peine de le faire
  préventivement.
- Accès SSH au Pi depuis ton PC (ou clavier/écran direct dessus).

## 2. Transférer le projet vers le Pi

Le projet n'est pas encore un dépôt git — pas la peine d'en créer un
juste pour ce transfert. Depuis ton PC (PowerShell ou l'Explorateur
Windows avec un client SFTP comme WinSCP/FileZilla), copie le dossier
`crypto_radar\` vers le Pi, **en excluant** ce qui suit (pas la peine
de transférer des données existantes ou des environnements déjà
installés) :

- `data\raw\`, `data\processed\`, `data\features\`, `data\models\` —
  le Pi va générer les siennes, pas la peine de transférer du poids
  pour rien.
- `.venv\` — sera recréé sur le Pi (environnement Windows ≠ Linux ARM,
  incompatible de toute façon).
- `__pycache__\`, `.pytest_cache\` si présents.

Dans les commandes ci-dessous, remplace `<user>` par ton utilisateur
sur le Pi et `<pi-host>` par son nom réseau (résolu en mDNS/Avahi, pas
besoin de connaître son IP). On colle `crypto_radar/` directement dans
le répertoire personnel : il finira à `/home/<user>/crypto_radar`.

**`rsync` n'existe pas nativement sous Windows/PowerShell** (c'est un
outil Linux/macOS) — inutile d'essayer de l'installer juste pour ça.
`scp`, en revanche, fait partie du client OpenSSH que Windows 10/11
inclut normalement d'origine. Vérifie qu'il est disponible :

```powershell
Get-Command scp -ErrorAction SilentlyContinue
```

Si rien ne s'affiche (commande introuvable), active la fonctionnalité
Windows correspondante — Paramètres → Applications → Fonctionnalités
facultatives → Ajouter une fonctionnalité → « Client OpenSSH », ou en
PowerShell administrateur :
`Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`.

`scp -r` ne sait pas exclure de sous-dossiers (contrairement à
`rsync`), donc pour ne pas transférer `data\`/`.venv\`/`__pycache__\`
(inutiles, et `.venv\` casserait tout — c'est un environnement Windows,
incompatible avec le Pi), on prépare d'abord une copie propre avec
`robocopy` (natif Windows, lui sait exclure des dossiers), puis on
l'envoie avec `scp` :

```powershell
robocopy . ..\crypto_radar_pi_staging\crypto_radar /E /XD data .venv __pycache__ .pytest_cache
scp -r ..\crypto_radar_pi_staging\crypto_radar <user>@<pi-host>.local:/home/<user>/
```

(la première commande se lance depuis `crypto_radar\` — `robocopy`
renvoie un code de sortie non nul même en cas de succès, c'est normal,
ce n'est pas une erreur.)

Une clé USB ou un client SFTP graphique (WinSCP, FileZilla) fonctionne
aussi si tu préfères — mêmes exclusions à respecter manuellement.

## 3. Installer l'environnement sur le Pi

En SSH sur le Pi (`ssh <user>@<pi-host>.local`), dans `/home/<user>/crypto_radar` :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --prefer-binary -r requirements.txt
```

`--prefer-binary` privilégie les wheels précompilés ARM64 déjà
disponibles sur PyPI pour pandas/numpy/scipy/scikit-learn/pyarrow
plutôt que de les recompiler depuis les sources (long et gourmand en
RAM sur un Pi 4GB). Si un paquet insiste pour compiler depuis les
sources et que ça échoue par manque de mémoire, augmente
temporairement le swap (`sudo dphys-swapfile swapoff`, éditer
`CONF_SWAPSIZE=2048` dans `/etc/dphys-swapfile`, puis
`sudo dphys-swapfile setup && sudo dphys-swapfile swapon`) le temps de
l'installation.

## 4. Configuration — rien de sensible n'est nécessaire

Pour cette collecte pure (RESEARCH mode, le mode par défaut), aucune
clé API n'est requise : Hyperliquid n'en demande pas, les flux RSS non
plus. Copie simplement `.env.example` vers `.env` sur le Pi et
laisse-le tel quel (vide) — **ne transfère pas tes vraies clés
Telegram/Kraken sur le Pi**, elles n'y servent à rien pour cette étape
et un appareil allumé en permanence est une surface d'exposition
inutile pour des credentials réels. `config/*.yaml` peut être copié
tel quel depuis le PC (mêmes actifs BTC/ETH/SOL, mêmes sources).

## 5. Lancer la collecte en continu

**Option simple** (si tu veux juste tester rapidement) : `tmux` ou
`screen` pour garder le process actif après déconnexion SSH :

```bash
tmux new -s radar
source .venv/bin/activate
python -m src.main
# Ctrl+B puis D pour détacher, `tmux attach -t radar` pour revenir
```

**Option robuste** (recommandée pour plusieurs jours sans surveillance
— survit à un reboot ou un crash) : un service systemd. Le fichier
`deploy/crypto-radar.service` livré avec ce guide utilise
`<user>`/`/home/<user>/crypto_radar` : remplace `<user>` par ton
utilisateur sur le Pi, puis :

```bash
sudo cp deploy/crypto-radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-radar
```

`Restart=on-failure` relance automatiquement le process s'il plante ;
`enable` le fait aussi redémarrer automatiquement si le Pi reboote
(coupure de courant, etc.) — utile sur plusieurs jours.

## 6. Vérifier que ça tourne, à distance

```bash
sudo systemctl status crypto-radar
sudo journalctl -u crypto-radar -f
```

Tu devrais voir le même affichage console que sur ton PC (prix
BTC/ETH/SOL en direct, flux RSS, etc.) défiler dans les logs.

## 7. Après quelques jours : arrêt propre et récupération

Arrête proprement (le shutdown de `main.py` vide les buffers Parquet en
attente — ne pas couper le Pi brutalement pendant que ça tourne encore
si évitable) :

```bash
sudo systemctl stop crypto-radar
```

Les données à rapatrier sont dans deux endroits, **les deux sont
nécessaires** (le SQLite contient les événements de news que
`FeatureEngine` va joindre aux données de marché — les colonnes news
se dégraderaient silencieusement à neutre sans lui, pas une erreur mais
une perte d'information) :

- `data/raw/` (Parquet — order book/trades Hyperliquid)
- `data/processed/crypto_radar.db` (SQLite — événements de news, entre
  autres)

```powershell
scp -r <user>@<pi-host>.local:/home/<user>/crypto_radar/data/raw .\data\raw_from_pi
scp <user>@<pi-host>.local:/home/<user>/crypto_radar/data/processed/crypto_radar.db .\data\crypto_radar_from_pi.db
```

(exécuté depuis ton PC, dans `crypto_radar\`, en PowerShell — pas de
`rsync` sous Windows, voir la note de l'étape 2 ; WinSCP fonctionne
aussi si tu préfères une interface graphique).

## 8. Fusionner (ou pas) avec des données déjà collectées sur le PC

**Recommandation : ne fusionne pas.** Si tu as déjà des données
collectées sur ton PC pour une période qui chevauche celle du Pi,
les fichiers Parquet sont nommés `part-00000.parquet`,
`part-00001.parquet`, ... — un compteur qui repart de zéro à chaque
lancement de `python -m src.main` (vérifié dans
`storage/parquet_store.py`), donc **copier le dossier du Pi
par-dessus celui du PC pour une même date risque d'écraser des
fichiers existants portant le même nom**. Traiter le Pi comme la
source unique pour ce cycle de test "complet et propre" (comme tu l'as
dit toi-même) évite complètement ce problème — remplace `data/raw/`
et `data/processed/crypto_radar.db` du PC par ceux du Pi plutôt que de
les fusionner.

Si tu veux vraiment combiner les deux un jour, c'est faisable sans
risque de perte silencieuse de données : renomme les fichiers du Pi en
`part-pi-NNNNN.parquet` avant de les copier dans les mêmes dossiers de
partition (le lecteur (`parquet_reader.py`) matche n'importe quel nom
commençant par `part-` et finissant par `.parquet`, et l'ordre de
lecture n'a pas d'importance — `replay.py` re-trie tout
chronologiquement par timestamp réel avant de rejouer les événements,
pas par ordre de fichier). Pour le SQLite en revanche, fusionner deux
bases nécessite un vrai travail SQL (pas juste copier le fichier) —
seulement si tu en as vraiment besoin.

## 9. Reprendre sur le PC

Une fois les données en place sous `data/raw/` et
`data/processed/crypto_radar.db` sur le PC, reprends
`docs/21_paper_trading_methodology.md` à partir de l'étape 2
(construction de la table de features), en utilisant les dates UTC
réellement couvertes par la collecte du Pi (mêmes précautions sur les
fuseaux horaires que d'habitude — vérifie les dossiers `date=...`
plutôt que de calculer à la main).
