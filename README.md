# Trading System

Reusable base environment for algorithmic trading strategies.
Paper trading only — Alpaca paper endpoint hardcoded.

## Activate environment
```
conda activate trading
```

## Recreate environment from scratch
```
conda create -n trading python=3.11 -y
conda activate trading
pip install -r requirements.txt
```

## Run tests
```
python tests/test_all.py
```

## Verify utilities
```
python config/utils.py
```

## Start dashboard
```
python dashboard/server.py
```
Then open http://localhost:8080

## Register heartbeat monitor (run as Administrator)
```
schedulers\register_monitor.bat
```

## IMPORTANT
- `config/credentials.json` must **never** be shared or committed to git.
- Always activate `conda activate trading` before running any script.
- Paper mode is enforced — `is_paper_mode()` must return True before any strategy runs.
