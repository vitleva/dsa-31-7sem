import os
from datetime import datetime, timezone

from flask import Flask
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class Visit(db.Model):
    __tablename__ = "visits"

    id = db.Column(db.Integer, primary_key=True)
    visit_time = db.Column(db.DateTime(timezone=True), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)


def create_app():
    app = Flask(__name__)

    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "visits_db")
    db_user = os.getenv("DB_USER", "app")
    db_password = os.getenv("DB_PASSWORD", "changeme")

    database_url = (
        f"postgresql://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.get("/hello")
    def hello():
        visit = Visit(
            visit_time=datetime.now(timezone.utc),
            ip_address=request_remote_addr(),
        )

        db.session.add(visit)
        db.session.commit()

        return "Hello", 200

    return app


def request_remote_addr():
    from flask import request

    return request.remote_addr or "unknown"


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)