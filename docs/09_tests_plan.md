# Tests

**753 tests `pytest`, tous hors ligne** : aucun accès réseau (RSS
simulé avec `httpx.MockTransport`, WebSocket avec des messages types,
base de données et Parquet dans des dossiers temporaires, Kraken avec
un faux transport).

```bash
python -m pytest -q
```

## Ce qui est testé

| Domaine | Fichiers | Exemples de vérifications |
|---|---|---|
| Configuration | `test_config.py` | Le mode par défaut reste `RESEARCH`, toutes les variables de `.env.example` existent |
| Collecte | `test_hyperliquid_ws.py`, `test_rss_collector.py` | Décodage des messages, reconnexion, redirections, respect de `robots.txt` |
| Carnet d'ordres | `test_orderbook_*.py`, `test_trade_flow.py`, `test_liquidity.py`, `test_sweeps_absorption.py` | Formules OBI, microprice, flux, murs, sweeps, absorption |
| Stockage | `test_parquet_*.py`, `test_database_storage.py`, `test_main_storage_wiring.py` | Écriture et relecture, idempotence |
| Actualités | `test_news_*.py` | Classification, sentiment, pertinence (faux positifs réels du flux SEC), déduplication, décroissance |
| Horloges | `test_clock_discipline.py` | Tout est en UTC ; ce qui est visible au temps `t` ne dépend que de l'heure de réception |
| Features | `test_feature_engine.py`, `test_replay.py`, `test_regime_detector.py` | Ajouter un événement futur ne change aucune ligne passée ; le mode incrémental donne exactement le même résultat que le mode batch |
| Modèles | `test_baseline_*.py`, `test_logistic_model.py`, `test_tree_model.py`, `test_news_model.py`, `test_calibration.py`, `test_feature_matrix.py`, `test_model_registry.py`, `test_model_persistence.py` | Aucune cible ne fuit dans les features, calibration, registre en ajout seul |
| Entraînement | `test_train_model.py` | Validation croisée purgée, contrôle au niveau événement, espérance nette de frais, promotion uniquement manuelle |
| Backtest | `test_backtest_metrics.py`, `test_event_backtester.py`, `test_orderbook_backtester.py`, `test_walk_forward.py` | Métriques, bootstrap, Monte Carlo, ruine, purge entre fenêtres |
| Signaux et stratégie | `test_direction.py`, `test_confidence.py`, `test_scoring.py`, `test_signal_engine.py`, `test_entry.py`, `test_exit.py`, `test_strategy_engine.py` | Seuils d'entrée, ordre des 7 sorties, une position à la fois |
| Risque et alertes | `test_risk_manager.py`, `test_position_sizing.py`, `test_alert_manager.py`, `test_telegram_*.py`, `test_main_telegram_wiring.py` | Kill switch prioritaire, anti-spam, message `/signals` sous la limite Telegram |
| Exécution | `test_execution_interface.py`, `test_paper_executor.py`, `test_kraken_executor.py`, `test_performance.py` | Les 6 combinaisons incomplètes de verrous Kraken restent inactives ; signature des requêtes |
| Paper trading | `test_trading_loop.py`, `test_main_paper_trading_wiring.py`, `test_main_*.py` | Les deux verrous du mode PAPER, cycle ouverture → fermeture complet |

Chaque bug trouvé pendant le projet a reçu son test de non-régression.
