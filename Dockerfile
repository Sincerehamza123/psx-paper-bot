FROM freqtradeorg/freqtrade:stable
USER root
WORKDIR /app
RUN mkdir -p /app/user_data/strategies /app/user_data/backtest_results && python -c "from urllib.request import urlopen,Request; u='https://raw.githubusercontent.com/ceyhanmolla/freqtrade-strategies/main/GeneticEngineV1.py'; open('/app/user_data/strategies/GeneticEngineV1.py','wb').write(urlopen(Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=60).read())"
COPY app.py /app/app.py
RUN chown -R ftuser:ftuser /app
USER ftuser
CMD ["python","/app/app.py"]
