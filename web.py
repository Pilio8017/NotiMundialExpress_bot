import os
from threading import Thread
from flask import Flask
import bot  # Esto ejecuta tu bot en segundo plano automáticamente

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running!"


def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    # Inicia el servidor web en un hilo paralelo para cumplir con Render
    t = Thread(target=run_web)
    t.start()
