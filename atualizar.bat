@echo off
echo Atualizando arquivos do bot...
git fetch origin claude/new-session-JNgPX
git checkout origin/claude/new-session-JNgPX -- config.py execution.py indicators.py main.py requirements.txt telegram_notifier.py trade_history.py backtest.py .env.example bch-trade.service setup.sh
echo.
echo Pronto! Arquivos atualizados com sucesso.
pause
