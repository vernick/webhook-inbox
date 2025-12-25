import json
import os
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, abort, redirect, render_template, request, url_for
from sqlalchemy import (
    Column, DateTime, Integer, Text, create_engine, desc, text as sql_text
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ----------------------------
# Config
# ----------------------------
print("initializing flask app")
app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://webhook_inbox_user:53kiIMXusmJyzciM1svvEAaT5MVhpksN@dpg-d56lkfn5r7bs73fm4380-a.virginia-postgres.render.com/webhook_inbox")
# Render Postgres URLs are commonly postgres://...; SQLAlchemy wants postgresql://...
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    # Local dev fallback (no persistence on Render Free—fine for local testing)
    DATABASE_URL = "sqlite:///local_dev.db"

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN")  # optional
VIEWER_USER = os.environ.get("VIEWER_USER")      # optional
VIEWER_PASS = os.environ.get("VIEWER_PASS")      # optional

# How many webhooks to keep (simple retention so it doesn’t grow forever)
MAX_EVENTS = int(os.environ.get("MAX_EVENTS", "500"))

# ----------------------------
# DB setup
# ----------------------------
Base = declarative_base()
#engine = create_engine(DATABASE_URL, pool_pre_ping=True)
print("creating engine")
engine = create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1))

SessionLocal = sessionmaker(bind=engine)

class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)
    received_at = Column(DateTime, nullable=False)
    method = Column(Text, nullable=False)
    path = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    headers_json = Column(Text, nullable=False)
    body_text = Column(Text, nullable=True)

print("initializing database")
def init_db():
    print("creating all tables")
    Base.metadata.create_all(engine)

init_db()
print("database initialized")
# ----------------------------
# Basic auth for the viewer UI
# ----------------------------
def _basic_auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # If not configured, don’t require auth.
        if not (VIEWER_USER and VIEWER_PASS):
            return fn(*args, **kwargs)

        auth = request.authorization
        if not auth or auth.username != VIEWER_USER or auth.password != VIEWER_PASS:
            return abort(401, description="Authentication required")
        return fn(*args, **kwargs)
    return wrapper

@app.errorhandler(401)
def _auth_error(_):
    return (
        "Auth required", 401,
        {"WWW-Authenticate": 'Basic realm="Webhook Inbox"'}
    )

# ----------------------------
# Helpers
# ----------------------------
def _is_json(content_type: str | None) -> bool:
    return bool(content_type) and "json" in content_type.lower()

def _pretty_json(text_payload: str) -> str | None:
    try:
        obj = json.loads(text_payload)
        return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception:
        return None

def _enforce_retention(db):
    # Delete older rows beyond MAX_EVENTS
    # Works on Postgres & SQLite.
    db.execute(
        sql_text("""
            DELETE FROM webhook_events
            WHERE id NOT IN (
                SELECT id FROM webhook_events
                ORDER BY received_at DESC
                LIMIT :limit
            )
        """),
        {"limit": MAX_EVENTS},
    )

# ----------------------------
# Routes
# ----------------------------
@app.get("/")
@_basic_auth_required
def inbox():
    db = SessionLocal()
    try:
        events = db.query(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(200).all()
        return render_template("inbox.html", events=events)
    finally:
        db.close()

@app.get("/event/<int:event_id>")
@_basic_auth_required
def event_detail(event_id: int):
    db = SessionLocal()
    try:
        event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
        if not event:
            abort(404)
        pretty = _pretty_json(event.body_text or "") if _is_json(event.content_type) else None
        headers = json.loads(event.headers_json)
        return render_template("event.html", event=event, headers=headers, pretty_json=pretty)
    finally:
        db.close()

@app.post("/webhook")
def webhook():
    # Optional: protect the receiver endpoint with a shared secret header
    # Sender sets: X-Webhook-Token: <token>
    if WEBHOOK_TOKEN:
        if request.headers.get("X-Webhook-Token") != WEBHOOK_TOKEN:
            abort(401)

    raw_body = request.get_data(cache=False, as_text=True)
    headers_dict = {k: v for k, v in request.headers.items()}

    db = SessionLocal()
    try:
        ev = WebhookEvent(
            received_at=datetime.now(timezone.utc),
            method=request.method,
            path=request.path,
            content_type=request.content_type,
            headers_json=json.dumps(headers_dict, ensure_ascii=False),
            body_text=raw_body,
        )
        db.add(ev)
        db.commit()

        _enforce_retention(db)
        db.commit()

        return {"ok": True, "id": ev.id}, 201
    finally:
        db.close()

@app.get("/healthz")
def healthz():
    return {"ok": True}

if __name__ == "__main__":
    # Local dev server
    app.run(host="0.0.0.0", port=5050, debug=True)

