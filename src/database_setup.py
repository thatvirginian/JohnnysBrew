import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load local .env file if present (Fallback for local HQ-PC-29 development)
load_dotenv(override=True)

def get_db_url():
    """Fetches database credentials uniformly from the system environment."""
    user = os.getenv('PGUSER')
    password = os.getenv('PGPASSWORD')
    host = os.getenv('PGHOST')
    port = os.getenv('PGPORT')
    database = os.getenv('PGDATABASE')
    
    # Simple sanity check to warn you if your environment variables didn't load
    if not all([user, password, host, port, database]):
        print("⚠️ WARNING: One or more database environment variables are missing!")

    return (
        f"postgresql+psycopg2://{user}:{password}"
        f"@{host}:{port}/{database}"
        f"?sslmode=require"
    )

# 1. Instantiate the Engine EXACTLY ONCE globally.
# This engine handles the connection pool across all Gunicorn threads safely.
DATABASE_URL = get_db_url()
db_engine = create_engine(
    DATABASE_URL,
    pool_size=10,           # Keep 10 connections open per worker
    max_overflow=20,        # Allow up to 20 more during heavy reporting spikes
    pool_recycle=300,       # Recycle connections before Azure kills idle pipes
    pool_pre_ping=True      # Transparently reconnect if a connection drops
)

def get_db_connection():
    """Returns the global engine asset. Flask routes will call this."""
    return db_engine
