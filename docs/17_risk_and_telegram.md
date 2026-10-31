# Risque et notifications Telegram

Code : `src/strategy/risk_manager.py`, `position_sizing.py`,
`src/notifications/alert_manager.py`, `telegram_bot.py`,
`telegram_live.py`.

## Gestionnaire de risque — `risk_manager.py`

Suit le capital, le PnL du jour (remis à zéro chaque jour UTC) et la
série de pertes. Bloque le trading, dans cet ordre de priorité :

1. kill switch activé manuellement ;
2. qualité d'exécution dégradée (slippage ou latence trop élevés) ;
3. perte journalière maximale atteinte ;
4. trop de pertes consécutives.

## Taille de position — `position_sizing.py`

```text
taille de base = capital × risque par trade / distance du stop
```

Elle est ensuite **seulement réduite** selon la confiance, la
volatilité et l'espérance, puis plafonnée à 5 % du capital. Sans stop
ou avec une espérance négative, la taille vaut 0.

## Alertes — `alert_manager.py`

5 types d'alertes seulement : signal à forte conviction, position
ouverte, position fermée, alerte de risque, rapport quotidien. Le reste
(chaque actualité, chaque tick) ne peut pas déclencher de notification.

Pour un signal, le filtre est plus strict que pour trader
(probabilité ≥ 0,68 et qualité A ; 0,75 en mode SNIPER). Un délai
minimal par actif et un filtre sur les probabilités presque identiques
évitent le spam. Un événement majeur peut passer outre le délai.

## Bot Telegram

Commandes : `/status`, `/performance`, `/positions`, `/signals`,
`/today`, `/risk`, `/mode`, `/stop`.

- `/stop` active le kill switch ; il existe aussi indépendamment de
  Telegram.
- Le bot ne répond qu'au `chat_id` configuré et ignore tous les autres.
- Il ne s'active que si `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` sont
  renseignés dans `.env`.

## Points de configuration laissés ouverts

1. En mode AGGRESSIVE, les signaux B sont autorisés, mais le filtre
   global exige la qualité A : les signaux B ne passent donc jamais.
   Mettre `required_signal_quality` à `None` pour les autoriser.
2. « Edge exceptionnel » en mode SNIPER est interprété comme une
   probabilité ≥ 0,75 : c'est un choix, pas une valeur validée.
