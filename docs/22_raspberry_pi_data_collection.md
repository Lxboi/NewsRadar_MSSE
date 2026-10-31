# Collecte sur Raspberry Pi

Faire tourner la collecte en continu sur un Raspberry Pi (mode
RESEARCH, aucune clé API), puis rapatrier les données sur le PC pour
l'entraînement.

## 1. Prérequis

- Raspberry Pi OS 64 bits, Python 3.11 ou plus (le code n'utilise rien
  de spécifique à 3.12).
- Accès SSH au Pi.

## 2. Copier le projet

Depuis le dossier du projet sur le PC (PowerShell), sans `data`,
`.venv` ni les caches. Le projet arrive sur le Pi dans
`/home/<user>/crypto_radar`, le chemin attendu par le service :

```powershell
robocopy . ..\crypto_radar_pi_staging\crypto_radar /E /XD data .venv __pycache__ .pytest_cache
scp -r ..\crypto_radar_pi_staging\crypto_radar <user>@<pi-host>.local:/home/<user>/
```

`robocopy` renvoie un code non nul même quand tout va bien. Si `scp`
manque, installer « Client OpenSSH » dans les fonctionnalités
facultatives de Windows.

## 3. Installer l'environnement sur le Pi

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --prefer-binary -e .
```

`--prefer-binary` évite de recompiler pandas, numpy, etc. Si
l'installation manque de mémoire, augmenter le swap temporairement.

## 4. Configuration

Copier `.env.example` en `.env` et le laisser vide. **Ne pas mettre de
vraies clés sur le Pi** : elles ne servent à rien pour la collecte.

## 5. Lancer en continu

Avec le service fourni (remplacer `<user>` dans le fichier) :

```bash
sudo cp deploy/crypto-radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-radar
```

Le service redémarre en cas de plantage et au démarrage du Pi.
Pour un test rapide, `tmux` suffit.

## 6. Vérifier

```bash
sudo systemctl status crypto-radar
sudo journalctl -u crypto-radar -f
```

## 7. Arrêter et récupérer les données

```bash
sudo systemctl stop crypto-radar
```

Les deux dossiers sont nécessaires :

```powershell
scp -r <user>@<pi-host>.local:/home/<user>/crypto_radar/data/raw .\data\raw_from_pi
scp <user>@<pi-host>.local:/home/<user>/crypto_radar/data/processed/crypto_radar.db .\data\crypto_radar_from_pi.db
```

## 8. Ne pas fusionner avec les données du PC

Les fichiers s'appellent `part-00000.parquet`, `part-00001.parquet`… à
chaque lancement : copier ceux du Pi par-dessus ceux du PC peut écraser
des fichiers. Remplacer plutôt `data/raw/` et la base SQLite du PC par
ceux du Pi. Pour fusionner malgré tout, renommer les fichiers du Pi en
`part-pi-NNNNN.parquet`.

## 9. Reprendre sur le PC

Suivre [`21_paper_trading_methodology.md`](21_paper_trading_methodology.md)
à partir de l'étape 2.
