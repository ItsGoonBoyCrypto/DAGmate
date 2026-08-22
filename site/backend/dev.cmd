@echo off
REM Local development ONLY. The dev routes (demo wallet, dev-mark-funded) are
REM off unless asked for, so they have to be switched on HERE rather than
REM being on by default and switched off at deploy time — the latter is how
REM a free-money button reaches a public host. Refused on mainnet regardless.
cd /d "%~dp0"
set DAGMATE_DEV_ROUTES=1
set DAGMATE_NETWORK_ID=testnet-10
python -m uvicorn main:app --host 127.0.0.1 --port 8800
