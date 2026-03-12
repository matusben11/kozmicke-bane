"""
WSGI entry point pre Render / Railway / Heroku.
"""
from web_server import app

if __name__ == "__main__":
    app.run()
