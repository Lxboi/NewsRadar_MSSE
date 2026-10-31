# Collecte Hyperliquid (WebSocket)

Hyperliquid est la source principale de microstructure : carnet L2,
transactions et bougies. Le projet reconstruit lui-même ses indicateurs
à partir des messages bruts.

## Connexion

- Adresse : `wss://api.hyperliquid.xyz/ws`
- Abonnements, par actif :

```json
{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC", "interval": "1m"}}
```

## Messages reçus

Chaque message a la forme `{"channel": ..., "data": ...}`.

| Canal | Contenu |
|---|---|
| `l2Book` | Carnet complet (instantané, pas un delta) : niveaux `{px, sz, n}` |
| `trades` | Liste de transactions `{side, px, sz, time, tid, ...}` |
| `candle` | Bougie OHLCV |
| `pong` | Réponse au ping |

## Maintien de la connexion

- Le serveur coupe après 60 s sans message : le collecteur envoie un
  ping toutes les 20 s.
- Sans message pendant 30 s, le collecteur force une reconnexion.
- Toute déconnexion déclenche une reconnexion avec délai croissant.

Le décodage est fait par `parse_hyperliquid_message()` et testé hors
ligne dans `tests/test_hyperliquid_ws.py`.

Source : [documentation Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket).
