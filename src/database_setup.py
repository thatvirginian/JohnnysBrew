import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv(override=True)

# This tracking variable holds our single pool instance once created
_db_engine = None

def get_db_url():
    """Fetches database credentials uniformly from the system environment."""
    user = os.getenv('PGUSER')
    password = os.getenv('PGPASSWORD')
    host = os.getenv('PGHOST')
    port = os.getenv('PGPORT')
    database = os.getenv('PGDATABASE')
    
    if not all([user, password, host, port, database]):
        print("⚠️ WARNING: One or more database environment variables are missing!")

    return (
        f"postgresql+psycopg2://{user}:{password}"
        f"@{host}:{port}/{database}"
        f"?sslmode=require"
    )

def get_db_connection():
    """Returns the global engine asset, instantiating it lazily on first call."""
    global _db_engine
    
    if _db_engine is None:
        # This code will execute at RUNTIME on the first web request,
        # ensuring all Azure Container Secrets are safely initialized.
        url = get_db_url()
        _db_engine = create_engine(
            url,
            pool_size=10,           
            max_overflow=20,        
            pool_recycle=300,       
            pool_pre_ping=True      
        )
    return _db_engine
