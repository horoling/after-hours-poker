FROM python:3.13-slim
WORKDIR /app
COPY requirements-multiplayer.txt ./
RUN pip install --no-cache-dir -r requirements-multiplayer.txt
COPY private_rooms.py multiplayer_app.py session_stats.py room_agents.py ./
COPY engine/__init__.py engine/bots.py engine/hands.py engine/preflop_equity.json ./engine/
COPY web/friends.html web/friends.css web/friends.js web/results.js ./web/
RUN useradd --uid 10001 --create-home poker && mkdir /data && chown poker:poker /data
USER poker
ENV ROOMS_DATABASE=/data/rooms.sqlite3 PORT=8780
EXPOSE 8780
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 60 multiplayer_app:app"]
