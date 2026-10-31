# Architecture asyncio

## Règles

1. **Une tâche par source.** Chaque collecteur tourne dans sa propre
   tâche `asyncio` avec sa propre connexion.
2. **Des files bornées entre les étapes.** Les collecteurs écrivent dans
   une `asyncio.Queue` ; chaque étape suivante lit la file précédente et
   écrit dans la suivante. Une file pleine est surveillée, jamais
   ignorée.
3. **Isolation des pannes.** Reconnexion automatique avec délai
   croissant et aléatoire, et un délai maximal sans message pour
   détecter une connexion morte.
4. **Données périmées = arrêt du trading.** Sans donnée de marché
   pendant 15 s, le trading est bloqué.
5. **Arrêt propre.** `main.py` gère SIGINT/SIGTERM, pour que `systemd`
   puisse arrêter le service proprement sur le Raspberry Pi.

## Pourquoi des files plutôt que des appels directs

Un calcul lent ne doit pas ralentir la lecture du WebSocket (sinon la
plateforme coupe la connexion). Les files rendent aussi l'ajout d'une
source très simple : il suffit d'une coroutine `run()` qui écrit dans la
file commune.

Code : `src/main.py` et `src/collectors/hyperliquid_ws.py`.
