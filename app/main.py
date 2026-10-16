import hashlib
import json
import logging
import os
import secrets
import traceback
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", "-"),
            "method": getattr(record, "method", None),
            "path": getattr(record, "path", None),
            "status_code": getattr(record, "status_code", None),
            "request_body": getattr(record, "request_body", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["stack_trace"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


log_path = Path("/var/log/app/app.log")
log_path.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("qa-api")
logger.setLevel(logging.INFO)
logger.propagate = False
file_handler = logging.FileHandler(log_path, encoding="utf-8")
file_handler.setFormatter(JsonFormatter())
logger.addHandler(file_handler)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://qa_user:qa_password@postgres:5432/qa_db",
)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PREMIUM_PRICE = Decimal("10.00")
app = FastAPI(title="QA Business API")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, digest_hex = stored.split("$", 1)
    calculated = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 120_000
    ).hex()
    return secrets.compare_digest(calculated, digest_hex)


def db_connection():
    return psycopg.connect(DATABASE_URL)


def public_user(row):
    return {
        "id": row[0],
        "username": row[1],
        "email": row[2],
        "age": row[3],
        "role": row[4],
        "status": row[5],
        "balance": float(row[6]),
        "is_premium": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
    }


def user_select(prefix: str = ""):
    p = f"{prefix}." if prefix else ""
    return f"""
        SELECT {p}id, {p}username, {p}email, {p}age, {p}role, {p}status, {p}balance, {p}is_premium, {p}created_at
        FROM users {prefix}
    """


def find_user_by_token(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                user_select("u")
                + " JOIN sessions s ON s.user_id = u.id "
                + "WHERE s.token_hash = %s AND s.expires_at > NOW()",
                (token_hash,),
            )
            return cursor.fetchone()


def auth_user(request: Request, admin_only: bool = False):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None, JSONResponse({"detail": "Bearer token required"}, status_code=401)
    user = find_user_by_token(authorization[7:].strip())
    if not user:
        return None, JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
    if user[5] != "active":
        return None, JSONResponse({"detail": "User is blocked"}, status_code=403)
    if admin_only and user[4] != "admin":
        return None, JSONResponse({"detail": "Admin role required"}, status_code=403)
    return user, None


@app.on_event("startup")
def initialize_database():
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) NOT NULL,
                    age INTEGER NOT NULL CHECK (age >= 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user'")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance NUMERIC(12, 2) NOT NULL DEFAULT 0")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash VARCHAR(64) NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute("SELECT id FROM users WHERE username = %s LIMIT 1", (ADMIN_USERNAME,))
            admin = cursor.fetchone()
            if not admin:
                cursor.execute(
                    """
                    INSERT INTO users (username, email, age, password_hash, role, balance, is_premium)
                    VALUES (%s, %s, 30, %s, 'admin', 1000, TRUE)
                    """,
                    (ADMIN_USERNAME, "admin@example.local", hash_password(ADMIN_PASSWORD)),
                )
        connection.commit()
    logger.info("Database initialized", extra={"trace_id": "system", "status_code": 200})


@app.middleware("http")
async def request_logging(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    body_bytes = await request.body()
    request_body = None
    if body_bytes:
        try:
            request_body = json.loads(body_bytes.decode("utf-8"))
            if isinstance(request_body, dict):
                request_body = {
                    key: "***MASKED***" if key.lower() in {"password", "token", "access_token"} else value
                    for key, value in request_body.items()
                }
        except Exception:
            request_body = "<non-json body>"

    async def receive():
        return {"type": "http.request", "body": body_bytes}

    request_with_body = Request(request.scope, receive=receive)
    request_with_body.state.trace_id = trace_id
    try:
        response = await call_next(request_with_body)
    except Exception:
        logger.exception(
            "Unhandled exception",
            extra={"trace_id": trace_id, "method": request.method, "path": request.url.path,
                   "status_code": 500, "request_body": request_body},
        )
        response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    response.headers["X-Request-ID"] = trace_id
    logger.info(
        "%s %s completed", request.method, request.url.path,
        extra={"trace_id": trace_id, "method": request.method, "path": request.url.path,
               "status_code": response.status_code, "request_body": request_body},
    )
    return response


class RegisterInput(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    age: int = Field(ge=18, le=120)


class LoginInput(BaseModel):
    username: str
    password: str


class AmountInput(BaseModel):
    amount: Decimal = Field(gt=0, le=10000)


class StatusInput(BaseModel):
    status: str = Field(pattern="^(active|blocked)$")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "qa-business-api"}


@app.post("/api/user")
def create_legacy_user(data: dict):
    username = data.get("username")
    age = data.get("age")
    if not isinstance(username, str) or not username:
        return JSONResponse({"detail": "username is required"}, status_code=422)
    if not isinstance(age, int) or age < 18:
        return JSONResponse({"detail": "User must be at least 18"}, status_code=400)
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (username, age) VALUES (%s, %s) RETURNING id",
                (username, age),
            )
            user_id = cursor.fetchone()[0]
        connection.commit()
    return {"id": user_id, "username": username, "age": age}


@app.post("/api/auth/register", status_code=201)
def register(data: RegisterInput):
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE username = %s OR email = %s LIMIT 1", (data.username, data.email))
            if cursor.fetchone():
                return JSONResponse({"detail": "Username or email already exists"}, status_code=409)
            cursor.execute(
                """INSERT INTO users (username, email, age, password_hash)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (data.username, data.email, data.age, hash_password(data.password)),
            )
            user_id = cursor.fetchone()[0]
        connection.commit()
    return {"id": user_id, "username": data.username, "status": "active"}


@app.post("/api/auth/login")
def login(data: LoginInput):
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, password_hash, status FROM users WHERE username = %s", (data.username,))
            user = cursor.fetchone()
            if not user or not verify_password(data.password, user[1]):
                return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
            if user[2] != "active":
                return JSONResponse({"detail": "User is blocked"}, status_code=403)
            token = secrets.token_urlsafe(32)
            cursor.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, NOW() + INTERVAL '2 hours')",
                (user[0], hashlib.sha256(token.encode()).hexdigest()),
            )
        connection.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": 7200}


@app.get("/api/me")
def me(request: Request):
    user, error = auth_user(request)
    if error:
        return error
    return public_user(user)


@app.post("/api/me/deposit")
def deposit(data: AmountInput, request: Request):
    user, error = auth_user(request)
    if error:
        return error
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s WHERE id = %s RETURNING balance", (data.amount, user[0]))
            balance = cursor.fetchone()[0]
        connection.commit()
    return {"status": "accepted", "deposited": float(data.amount), "balance": float(balance)}


@app.post("/api/me/premium")
def activate_premium(request: Request):
    user, error = auth_user(request)
    if error:
        return error
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT balance, is_premium FROM users WHERE id = %s FOR UPDATE", (user[0],))
            balance, is_premium = cursor.fetchone()
            if is_premium:
                return {"status": "already_active", "balance": float(balance)}
            if balance < PREMIUM_PRICE:
                return JSONResponse({"detail": "Insufficient balance", "required": float(PREMIUM_PRICE)}, status_code=402)
            cursor.execute("UPDATE users SET balance = balance - %s, is_premium = TRUE WHERE id = %s RETURNING balance", (PREMIUM_PRICE, user[0]))
            balance = cursor.fetchone()[0]
        connection.commit()
    return {"status": "premium_active", "charged": float(PREMIUM_PRICE), "balance": float(balance)}


@app.get("/api/admin/users")
def admin_users(request: Request):
    user, error = auth_user(request, admin_only=True)
    if error:
        return error
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(user_select() + " ORDER BY id")
            users = [public_user(row) for row in cursor.fetchall()]
    return {"items": users, "count": len(users)}


@app.patch("/api/admin/users/{user_id}/status")
def update_user_status(user_id: int, data: StatusInput, request: Request):
    admin, error = auth_user(request, admin_only=True)
    if error:
        return error
    with closing(db_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET status = %s WHERE id = %s RETURNING id, username, status",
                (data.status, user_id),
            )
            updated = cursor.fetchone()
        connection.commit()
    if not updated:
        return JSONResponse({"detail": "User not found"}, status_code=404)
    return {"id": updated[0], "username": updated[1], "status": updated[2]}


@app.get("/api/error")
def deliberate_error():
    raise RuntimeError("Deliberate test error")
